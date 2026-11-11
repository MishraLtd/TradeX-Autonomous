"""Private, reproducible reports and complete single-database snapshots."""
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .config import D, dumps, TrialConfig, LEGACY_FEES
from .ledger import summarize, Ledger
from .costs import breakdown, fee_delta, fee


def write_private(path, value, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(dumps(value) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        return
    temp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    try:
        with open(temp, 'w', opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            handle.write(dumps(value) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def report(path, now=None):
    now = time.time() if now is None else now
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        state = json.loads(db.execute('SELECT value FROM trial WHERE id=1').fetchone()[0])
        events = [{**json.loads(data), 'at': at, 'kind': kind} for at, kind, data in db.execute(
            'SELECT at,kind,data FROM events ORDER BY id')]
        samples = [dict(at=at, equity=value, stale=bool(stale)) for at, value, stale in db.execute(
            'SELECT at,value,stale FROM equity ORDER BY at')]
    result = summarize(state, now)
    closed = [t for t in result['trades'] if t['state'] == 'CLOSED']
    pnl = [D(t['gross_pnl']) - D(t['entry_fees']) - D(t['exit_fees']) for t in closed]
    fills = [e for e in events if e['kind'] == 'PAPER_FILL']
    # Reconstruct old fee components using their original schedule. This is
    # reporting only: historical fills, charged fees and cash are never rewritten.
    totals, exit_turnovers, phases = {}, {}, {}
    for fill in fills:
        trade = state['trades'][fill['trade']]
        model = trade.get('fee_model', LEGACY_FEES)
        before = exit_turnovers.get(trade['id'], D(0)) if fill['leg'] == 'EXIT' else D(0)
        after = before + D(fill['price']) * fill['quantity']
        parts = fee_delta(fill['side'], before, after, model)
        fill['fee_components'] = {k: str(v) for k, v in parts.items()}
        fill['fee_model'] = model
        if fill['leg'] == 'EXIT':
            exit_turnovers[trade['id']] = after
        for k, v in parts.items():
            totals[k] = totals.get(k, D(0)) + v
    valuation = object.__new__(Ledger)
    valuation.s, valuation.cfg = state, TrialConfig(**state['config'])
    for trade in result['trades']:
        model = trade.get('fee_model', LEGACY_FEES)
        policy = trade.get('exit_policy', 'FIXED_PERCENT')
        opening, closing_side = ('BUY', 'SELL') if trade['direction'] == 'LONG' else ('SELL', 'BUY')
        trade['entry_fee_components'] = {k: str(v) for k, v in breakdown(
            opening, D(trade['entry_price']) * trade['quantity'], model).items()}
        turnover = D(trade['exit_turnover'])
        quote = state['quotes'].get(trade['symbol'])
        if trade['remaining'] and quote:
            turnover += valuation.exit_price(trade, quote) * trade['remaining']
        trade['estimated_exit_fee_components'] = {k: str(v) for k, v in breakdown(closing_side, turnover, model).items()}
        trade['estimated_total_fees'] = str(D(trade['entry_fees']) + fee(closing_side, turnover, model))
        trade['net_pnl_estimate'] = str(valuation.net_liquidation(trade))
        trade['gross_pnl_estimate'] = str(valuation.gross_liquidation(trade))
        trade['loss_basis'] = ('PRICE' if policy == 'NET_PROFIT_PRICE_STOP' else
                               'NET' if policy == 'NET_CASH' else 'PRICE_PERCENT')
        trade['valuation_fresh'] = not trade['remaining'] or valuation.fresh(trade['symbol'], now)
        entry_policy = trade.get('entry_policy', 'FIXED_SCHEDULE')
        phase = phases.setdefault(entry_policy + ':' + policy + ':' + model,
                                  dict(entry_policy=entry_policy, exit_policy=policy, fee_model=model,
                                  trades=0, closed_trades=0, realized_net_closed=D(0)))
        phase['trades'] += 1
        if trade['state'] == 'CLOSED':
            phase['closed_trades'] += 1
            phase['realized_net_closed'] += D(trade['net_pnl_estimate'])
    latency = sorted(e['at'] - e['requested_at'] for e in fills if e['leg'] == 'EXIT')
    peak, drawdown = D(state['config']['initial_cash']), D(0)
    for row in samples:
        if not row['stale']:
            peak = max(peak, D(row['equity']))
            drawdown = max(drawdown, peak - D(row['equity']))
    expected_seconds = 0
    from .config import stamp
    for day in state['config']['sessions']:
        expected_seconds += max(0, min(now, stamp(day, '15:10')) - stamp(day, '09:15'))
    result.update(
        report_schema=2, generated_at=now, assumptions=state['config'],
        paid_fee_components={k: str(v) for k, v in totals.items()},
        policy_results=[{**p, 'realized_net_closed': str(p['realized_net_closed'])} for p in phases.values()],
        evidence='LOCAL_SIMULATION_ON_UPSTOX_DATA', closed_trades=len(closed),
        wins=sum(x > 0 for x in pnl), losses=sum(x < 0 for x in pnl),
        win_rate=sum(x > 0 for x in pnl) / len(closed) if closed else None,
        net_return_pct=str((D(result['equity_estimate']) / D(result['initial_cash']) - 1) * 100),
        realized_net_closed=str(sum(pnl, D(0))),
        sampled_max_drawdown=str(drawdown), equity_sample_interval_seconds=60,
        exit_fill_latency_max=max(latency, default=None),
        exit_fill_latency_p95=latency[math.ceil(.95 * len(latency)) - 1] if latency else None,
        slippage_cost_estimate=str(sum((D(e.get('slippage_cost', 0)) for e in fills), D(0))),
        expected_session_seconds=expected_seconds,
        feed_coverage_ratio=min(1, state['feed_fresh_seconds'] / expected_seconds) if expected_seconds else None,
        exit_failures=[e for e in events if e['kind'] == 'INCIDENT'], fills=fills, events=events,
        equity_samples=samples,
        limitations=['Fees and fills are estimates, not broker executions.',
                     'Depth is a top-of-book observation, not guaranteed available liquidity.',
                     'One week of fixed entries tests operations, not a profitable strategy.',
                     'Net exit thresholds trigger requests; gaps, latency and partial fills can breach them.',
                     'The price-loss stop excludes fees/taxes; account P&L and the daily loss guard include them.',
                     'Policy changes split this experiment into distinct phases; compare them separately.',
                     'Stale or unresolved positions are never assigned invented closing fills.'])
    return result


def snapshot(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as src, \
                closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Trial snapshot integrity failed')
            state = json.loads(dst.execute('SELECT value FROM trial WHERE id=1').fetchone()[0])
        with destination.open('rb') as handle:
            os.fsync(handle.fileno())
        meta = dict(schema=1, kind='UPSTOX_PAPER_TRIAL', created_at=time.time(),
                    config_hash=state['config_hash'], sha256=hashlib.sha256(destination.read_bytes()).hexdigest())
        write_private(str(destination) + '.json', meta, exclusive=True)
        return meta
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def restore(source, destination, config):
    source = Path(source)
    meta = json.loads(Path(str(source) + '.json').read_text())
    if meta.get('kind') != 'UPSTOX_PAPER_TRIAL' or meta['config_hash'] != config.fingerprint or \
            hashlib.sha256(source.read_bytes()).hexdigest() != meta['sha256']:
        raise ValueError('Trial backup checksum or frozen config mismatch')
    from .ledger import read_state
    from .config import TrialConfig
    embedded = read_state(source)
    if embedded['config_hash'] != config.fingerprint or TrialConfig(**embedded['config']).fingerprint != config.fingerprint:
        raise ValueError('Embedded trial configuration mismatch')
    snapshot(source, destination)
    with closing(sqlite3.connect(destination)) as db, db:
        state = json.loads(db.execute('SELECT value FROM trial WHERE id=1').fetchone()[0])
        if state['config_hash'] != config.fingerprint:
            raise ValueError('Embedded trial configuration mismatch')
        state.update(halt='RESTORED_TRIAL_REQUIRES_REVIEW', heartbeat=0)
        db.execute('UPDATE trial SET value=? WHERE id=1', (dumps(state),))
    restored_meta = dict(meta, sha256=hashlib.sha256(Path(destination).read_bytes()).hexdigest(),
                         restored_from=meta['sha256'], restored_at=time.time())
    write_private(str(destination) + '.json', restored_meta)
    return dict(status='RESTORED_HALTED', config_hash=config.fingerprint)


def daily_report(result, day):
    """Day-scoped trade evidence, without labelling today's cash as past cash."""
    from .config import stamp
    trades = [t for t in result['trades'] if t['day'] == day]
    closed = [t for t in trades if t['state'] == 'CLOSED']
    return dict(report_schema=2, day=day, generated_at=result['generated_at'],
                evidence=result['evidence'], config_hash=result['config_hash'],
                session=result['sessions'].get(day, 'PLANNED'), trades=trades,
                closed_net_estimate=str(sum((D(t['gross_pnl']) - D(t['entry_fees']) - D(t['exit_fees']) for t in closed), D(0))),
                unresolved=[t['id'] for t in trades if t['remaining']],
                slots=[s for s in result['slots'] if s['day'] == day],
                fills=[f for f in result['fills'] if f['trade'].startswith(day + ':')],
                equity_samples=[s for s in result['equity_samples'] if stamp(day, '00:00') <= s['at'] < stamp(day, '23:59')],
                limitations=result['limitations'])
