"""Explicit, offline, flat-account policy migration; never resets a trial."""
from contextlib import closing
from dataclasses import asdict, replace
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .config import TrialConfig, CURRENT_FEES, D, dumps, implementation_fingerprint, stamp
from .ledger import read_state
from .report import snapshot, write_private


def upgrade(config_path, db_path, backup_dir, expected_implementation, release, now=None, check=False, expand_universe=False, random_rotation=False, trend_recovery=False):
    now = time.time() if now is None else now
    config_path, db_path, backup_dir = map(Path, (config_path, db_path, backup_dir))
    if not re.fullmatch(r'[a-f0-9]{64}', expected_implementation) or not re.fullmatch(r'[a-f0-9]{40}', release):
        raise ValueError('Reviewed source fingerprint and full target release SHA are required')
    # Same lifetime lock as the worker. Holding it across backup, config replace
    # and the SQL commit prevents a worker from observing an intermediate pair.
    with open(str(db_path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('Stop the paper worker before upgrading') from None
        cfg = TrialConfig.load(config_path)
        old_config = json.loads(config_path.read_text())
        state = read_state(db_path)
        if cfg.fingerprint != state['config_hash'] or TrialConfig(**state['config']).fingerprint != cfg.fingerprint:
            raise ValueError('Source config/ledger mismatch; inspect the preserved backup before retrying')
        if state['implementation_sha256'] != expected_implementation:
            raise ValueError('Source code fingerprint does not match the running trial')
        if cfg.exit_policy not in ('FIXED_PERCENT', 'NET_CASH', 'NET_PROFIT_PRICE_STOP', 'NET_RECOVERY'):
            raise ValueError('Unsupported source policy')
        if state['halt'] or any(t['remaining'] or t['state'] != 'CLOSED' for t in state['trades'].values()):
            raise ValueError('Upgrade requires a flat account with no unresolved incident')
        if any(s['state'] == 'PENDING' for s in state['slots'].values()):
            raise ValueError('Upgrade requires no pending entries')
        if any(stamp(day, '09:10') <= now <= stamp(day, '15:15') for day in cfg.sessions):
            raise ValueError('Upgrade outside the planned session: before 09:10 or after 15:15 IST')
        if now >= stamp(cfg.sessions[-1], cfg.hard_close_time):
            raise ValueError('The trial has ended; preserve its report instead of extending its dates')
        if trend_recovery and random_rotation:
            raise ValueError('Select one entry policy')
        policy_change = cfg.exit_policy not in ('NET_PROFIT_PRICE_STOP', 'NET_RECOVERY')
        new_cfg = (replace(cfg, exit_policy='NET_PROFIT_PRICE_STOP', net_profit_inr='1', price_loss_inr='2',
                           recycle_entries=True, fee_model=CURRENT_FEES) if policy_change else cfg)
        if trend_recovery:
            from .trend import SETTINGS
            policy_change = policy_change or cfg.entry_policy != 'TREND_ROTATION'
            new_cfg = replace(new_cfg, entry_policy='TREND_ROTATION', exit_policy='NET_RECOVERY',
                              entry_times=(), net_profit_inr='5', trend_settings=dict(SETTINGS),
                              recycle_entries=True, fee_model=CURRENT_FEES)
        universe_change = False
        if expand_universe or random_rotation or trend_recovery:
            from .upstox import expanded_instruments, APPROVED_SYMBOLS, APPROVED_TIMES
            symbols = tuple(sorted(cfg.instruments, key=lambda s: cfg.instruments[s]['slot']))
            if symbols not in (APPROVED_SYMBOLS[:3], APPROVED_SYMBOLS):
                raise ValueError('Expansion requires the original three or the reviewed eight stocks')
            resolved = expanded_instruments()
            if any(resolved[s] != row for s, row in cfg.instruments.items()):
                raise ValueError('Existing instrument metadata changed; inspect before upgrading')
            if symbols != APPROVED_SYMBOLS:
                effective = next(day for day in cfg.sessions if now < stamp(day, '09:10'))
                new_cfg = replace(new_cfg, instruments=resolved,
                                  entry_times=() if new_cfg.entry_policy != 'FIXED_SCHEDULE' else APPROVED_TIMES,
                                  universe_effective_date=effective)
                universe_change = True
        if random_rotation:
            policy_change = policy_change or cfg.entry_policy != 'RANDOM_ROTATION'
            new_cfg = replace(new_cfg, entry_policy='RANDOM_ROTATION', entry_times=())
        if not policy_change and not universe_change and state['implementation_sha256'] == implementation_fingerprint():
            raise ValueError('This release and corrected policy are already active')
        cash = D(cfg.initial_cash) + D(state['realized_gross']) - D(state['fees'])
        final_status = 'UNIVERSE_UPGRADED' if universe_change else 'POLICY_UPGRADED' if policy_change else 'RELEASE_UPGRADED'
        result = dict(status='UPGRADE_CHECKED' if check else final_status,
                      cash_preserved=str(cash), trades_preserved=len(state['trades']),
                      sessions_preserved=cfg.sessions, config_hash=new_cfg.fingerprint,
                      exit_policy=new_cfg.exit_policy, loss_basis='NET_RECOVERY' if new_cfg.exit_policy == 'NET_RECOVERY' else 'PRICE',
                      price_loss_inr=None if new_cfg.exit_policy == 'NET_RECOVERY' else new_cfg.price_loss_inr,
                      net_profit_inr=new_cfg.net_profit_inr, trend_settings=new_cfg.trend_settings,
                      release=release, backup=str(backup_dir))
        result.update(instruments=new_cfg.instruments, entry_times=new_cfg.entry_times,
                      universe_effective_date=new_cfg.universe_effective_date, entry_policy=new_cfg.entry_policy)
        if check:
            return result
        backup_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        snapshot(db_path, backup_dir / 'trial.sqlite')
        write_private(backup_dir / 'config.json', old_config, exclusive=True)
        receipt = dict(kind='UNIVERSE_UPGRADE' if universe_change else 'POLICY_UPGRADE' if policy_change else 'RELEASE_UPGRADE',
                       at=now, from_config_hash=state['config_hash'], to_config_hash=new_cfg.fingerprint,
                       from_config=state['config'], to_config=asdict(new_cfg),
                       from_implementation=state['implementation_sha256'],
                       to_implementation=implementation_fingerprint(),
                       from_release=state['release'], to_release=release,
                       cash_preserved=str(cash), trades_preserved=len(state['trades']))
        write_private(backup_dir / 'upgrade.json', receipt, exclusive=True)
        old_stat = config_path.stat()

        def write_config(value):
            write_private(config_path, value)
            os.chmod(config_path, old_stat.st_mode & 0o777)
            if os.geteuid() == 0:
                os.chown(config_path, old_stat.st_uid, old_stat.st_gid)

        with closing(sqlite3.connect(db_path, timeout=10)) as db:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            try:
                changes = dict(schema=2, config=asdict(new_cfg), config_hash=new_cfg.fingerprint,
                               implementation_sha256=implementation_fingerprint(), release=release)
                if trend_recovery and cfg.entry_policy != 'TREND_ROTATION':
                    changes.update(rotation={}, trend_history={})
                if policy_change or universe_change:
                    changes['policy_history'] = state.get('policy_history', []) + [receipt]
                else:
                    changes['release_history'] = state.get('release_history', []) + [receipt]
                state.update(changes)
                db.execute('UPDATE trial SET value=? WHERE id=1', (dumps(state),))
                db.execute('INSERT INTO events(at,kind,data) VALUES (?,?,?)', (now, receipt['kind'], dumps(receipt)))
                write_config(asdict(new_cfg))
                db.commit()
            except Exception:
                db.rollback()
                write_config(old_config)
                raise
        return result
