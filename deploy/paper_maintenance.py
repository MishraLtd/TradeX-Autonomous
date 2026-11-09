"""Publish paper/feed health; back up the complete trial and private report."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time

from execution.paper_trial.config import TrialConfig, dumps, implementation_fingerprint
from execution.paper_trial.ledger import read_state, summarize
from execution.paper_trial.report import report, snapshot, write_private
from execution.operations import blob_client


def maintain(action):
    if action not in ('health', 'backup'):
        raise ValueError('Unknown maintenance action')
    db = Path(os.environ.get('TRADEX_TRIAL_DB', '/var/lib/tradex/upstox-trial/trial.sqlite'))
    config_path = Path(os.environ.get('TRADEX_TRIAL_CONFIG', '/var/lib/tradex/upstox-trial/config.json'))
    base = os.environ['TRADEX_OPERATIONS_URL'].rstrip('/')
    receipt = db.parent / 'backup-receipt.json'
    if action == 'health':
        now = time.time()
        payload = dict(checked_at=now, status='DEGRADED', codes=['TRIAL_STATE_UNAVAILABLE'],
                       account=os.environ['TRADEX_ACCOUNT_ID'], mode='UPSTOX_DATA_LOCAL_PAPER')
        try:
            cfg = TrialConfig.load(config_path)
            state = read_state(db)
            health = summarize(state, now)
            payload.update(status=health['status'], codes=list(health['codes']), completed=health['completed'])
            if state['config_hash'] != cfg.fingerprint:
                payload['codes'].append('TRIAL_CONFIG_CHANGED')
            if state['implementation_sha256'] != implementation_fingerprint():
                payload['codes'].append('TRIAL_CODE_CHANGED')
            try:
                last = json.loads(receipt.read_text())
                if last['config_hash'] != cfg.fingerprint or not 0 <= now - last['uploaded_at'] <= 27 * 3600:
                    raise ValueError()
            except (OSError, ValueError, KeyError):
                payload['codes'].append('TRIAL_BACKUP_MISSING_OR_STALE')
            if payload['codes']:
                payload['status'] = 'DEGRADED'
            # This container is private. The externally checked health blob
            # contains no balances, trades, tokens or signed market-data URLs.
            report_receipt = db.parent / 'report-upload.json'
            try:
                published = json.loads(report_receipt.read_text())
            except (OSError, ValueError):
                published = {}
            if (published.get('config_hash') != cfg.fingerprint or
                    now - published.get('uploaded_at', 0) >= 3600 or
                    (health['completed'] and not published.get('completed'))):
                # Reuse the existing backups/ Azure lifecycle prefix. Hourly
                # reports avoid minute-by-minute versioned financial snapshots.
                blob_client(base + '/backups/upstox-trial/latest-report.json').upload_blob(dumps(report(db, now)), overwrite=True)
                write_private(report_receipt, dict(config_hash=cfg.fingerprint, uploaded_at=now,
                                                   completed=health['completed']))
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            payload.update(status='DEGRADED', codes=['TRIAL_STATE_UNAVAILABLE'])
        blob_client(base + '/health.json').upload_blob(dumps(payload), overwrite=True)
        return payload
    cfg = TrialConfig.load(config_path)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())
    destination = db.parent / 'backups' / (name + '.sqlite')
    meta = snapshot(db, destination)
    if meta['config_hash'] != cfg.fingerprint:
        raise ValueError('Trial config/backup mismatch')
    report_path = destination.with_suffix('.report.json')
    write_private(report_path, report(destination), exclusive=True)
    # The manifest is the last uploaded object and signals a complete set.
    for source in (destination, report_path, Path(str(destination) + '.json')):
        with source.open('rb') as handle:
            blob_client(base + '/backups/upstox-trial/' + source.name).upload_blob(handle, overwrite=False)
    write_private(receipt, dict(config_hash=cfg.fingerprint, uploaded_at=time.time(),
                                backup=name, sha256=meta['sha256']))
    for source in destination.parent.iterdir():
        if source.is_file() and time.time() - source.stat().st_mtime > 7 * 86400:
            source.unlink()
    return dict(status='BACKED_UP', config_hash=cfg.fingerprint)
