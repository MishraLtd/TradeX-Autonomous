"""Independent health publication and nightly consistent backup; no trading calls."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Phase2A_Execution'))
from execution.models import Config, dumps
from execution.store import Store
from execution.supervision import health_snapshot
from execution.operations import backup, blob_client

cfg = Config.load(os.environ.get('TRADEX_CONFIG', '/var/lib/tradex/config.json'))
base = os.environ['TRADEX_OPERATIONS_URL'].rstrip('/')
state = Path('/var/lib/tradex')
if sys.argv[1] == 'health':
    store = Store(state / 'engine.sqlite')
    try:
        health = health_snapshot(store, cfg, time.time())
        payload = dict(checked_at=health['checked_at'], status=health['status'],
                       codes=[a['code'] for a in health['alerts']], account=os.environ['TRADEX_ACCOUNT_ID'])
        blob_client(base + '/health.json').upload_blob(dumps(payload), overwrite=True)
    finally:
        store.close()
else:
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + str(time.time_ns())
    path = state / 'backups' / (name + '.sqlite')
    backup(state / 'engine.sqlite', path)
    for source in (path, Path(str(path) + '.json')):
        with source.open('rb') as handle:
            blob_client(base + '/backups/' + source.name).upload_blob(handle, overwrite=False)
    config = dict(config=asdict(cfg))
    if cfg.cost_profile:
        config['cost_profile'] = json.loads(Path(cfg.cost_profile).read_text())
    blob_client(base + '/backups/' + name + '.config.json').upload_blob(dumps(config), overwrite=False)
    for source in (state / 'backups').glob('*'):
        if source.is_file() and time.time() - source.stat().st_mtime > 7 * 86400:
            source.unlink()
