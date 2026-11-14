#!/usr/bin/env bash
# Run on the VM, as root, from the pinned repository checkout.
# Token preflight completes before stopping the previous paper worker.
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run with sudo on the Azure VM'; exit 1; }
root=$(cd "$(dirname "$0")/../.." && pwd)
release=${1:?Supply the full reviewed Git commit SHA}
[[ $release =~ ^[a-f0-9]{40}$ ]] || { echo 'A full Git SHA is required'; exit 1; }
[[ $(git -C "$root" rev-parse HEAD) == "$release" ]] || { echo 'Checkout does not match requested release'; exit 1; }
[[ -z $(git -C "$root" status --porcelain --untracked-files=normal) ]] || { echo 'Use a clean pinned checkout'; exit 1; }
[[ -f /etc/tradex/worker.env ]] || { echo 'Existing cloud worker.env is required'; exit 1; }
set -a
# Root-owned operator configuration; never print its contents.
source /etc/tradex/worker.env
set +a
[[ ${TRADEX_MODE:-paper} == paper || ${TRADEX_MODE:-} == upstox-paper ]] || {
  echo 'Refusing to switch a live worker. This installer is paper-only.'; exit 1;
}
: "${TRADEX_VAULT_URL:?}" "${TRADEX_LEASE_URL:?}" "${TRADEX_OPERATIONS_URL:?}" "${TRADEX_ACCOUNT_ID:?}"
candidate="/opt/tradex/releases/$release"
if [[ ! -d $candidate ]]; then
  bash "$root/deploy/bootstrap.sh" "$release" --stage-only
fi
python="$candidate/venv/bin/python"
state=/var/lib/tradex/upstox-trial
if [[ -f $state/trial.sqlite ]]; then
  echo 'A trial ledger already exists; refusing to reset money, change code or restart its week.'
  echo 'Use the status/report commands in deploy/UPSTOX_PAPER.md.'
  exit 1
fi
install -d -m 700 -o tradex -g tradex "$state"
cd "$candidate/Phase2A_Execution"
if [[ ! -f $state/config.json ]]; then
  runuser -u tradex -- env TRADEX_MODE=upstox-paper TRADEX_VAULT_URL="$TRADEX_VAULT_URL" \
    "$python" -m execution.paper_trial --config "$state/config.json" --db "$state/trial.sqlite" prepare
fi
"$python" - <<'PY'
from execution.paper_trial.config import TrialConfig, stamp
from execution.paper_trial.MarketDataFeed_pb2 import FeedResponse
import websocket
import time
cfg = TrialConfig.load('/var/lib/tradex/upstox-trial/config.json')
assert cfg.entry_policy == 'TREND_PORTFOLIO' and cfg.per_trade_limit is None and cfg.max_positions == 3
assert time.time() < stamp(cfg.sessions[0], '09:25'), 'Prepared start date has passed; inspect config before proceeding'
PY
previous=$(readlink -f /opt/tradex/current)
saved_env="/etc/tradex/worker.env.before-upstox.$(date -u +%Y%m%dT%H%M%S)"
cp -p /etc/tradex/worker.env "$saved_env"
api_was_active=$(systemctl is-active tradex-api.service || true)
api_was_enabled=$(systemctl is-enabled tradex-api.service || true)
rollback() {
  echo 'Activation failed; restoring the previous paper worker configuration.' >&2
  systemctl stop tradex.service || true
  cp -p "$saved_env" /etc/tradex/worker.env
  ln -sfn "$previous" /opt/tradex/current
  install -m 644 "$previous"/deploy/systemd/* /etc/systemd/system/
  systemctl daemon-reload
  systemctl start tradex.service tradex-health.timer tradex-backup.timer || true
  if [[ $api_was_enabled == enabled ]]; then systemctl enable tradex-api.service || true; fi
  if [[ $api_was_active == active ]]; then systemctl start tradex-api.service || true; fi
}
trap rollback ERR
systemctl stop tradex-api.service tradex.service tradex-health.timer tradex-backup.timer
systemctl stop tradex-maintain@health.service tradex-maintain@backup.service
"$python" - <<'PY'
from contextlib import closing
from datetime import datetime, timezone
import hashlib, json, os, shutil, sqlite3
from pathlib import Path
state = Path('/var/lib/tradex')
with closing(sqlite3.connect((state / 'engine.sqlite').as_uri() + '?mode=ro', uri=True)) as db:
    trades = [json.loads(r[0]) for r in db.execute('SELECT data FROM trades')]
    if any(t.get('state') not in ('CLOSED', 'REJECTED', 'CANCELLED') or t.get('remaining', 0) for t in trades):
        raise SystemExit('Existing DEMO account is not flat; switch cancelled')
dest = state / ('legacy-before-upstox-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S'))
dest.mkdir(mode=0o700)
for name in ('engine.sqlite', 'paper.sqlite'):
    with closing(sqlite3.connect((state / name).as_uri() + '?mode=ro', uri=True)) as source, \
            closing(sqlite3.connect(dest / name)) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
shutil.copyfile(state / 'config.json', dest / 'config.json')
(dest / 'manifest.json').write_text(json.dumps({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in dest.iterdir()}))
env = Path('/etc/tradex/worker.env')
lines = [l for l in env.read_text().splitlines() if not l.startswith(('TRADEX_MODE=', 'TRADEX_TRIAL_CONFIG=', 'TRADEX_TRIAL_DB='))]
lines += ['TRADEX_MODE=upstox-paper', 'TRADEX_TRIAL_CONFIG=/var/lib/tradex/upstox-trial/config.json',
          'TRADEX_TRIAL_DB=/var/lib/tradex/upstox-trial/trial.sqlite']
env.write_text('\n'.join(lines) + '\n')
os.chmod(env, 0o640)
PY
chown root:tradex "$state/config.json"
chmod 640 "$state/config.json"
ln -sfn "$candidate" /opt/tradex/current
install -m 644 "$candidate"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tradex.service
systemctl disable tradex-api.service
for attempt in {1..20}; do
  [[ -f $state/trial.sqlite ]] && break
  sleep 1
done
[[ -f $state/trial.sqlite ]] || { echo 'Paper worker did not create its ledger'; false; }
systemctl start tradex-maintain@backup.service
systemctl start tradex-maintain@health.service
systemctl enable --now tradex-health.timer tradex-backup.timer
trap - ERR
echo 'Paper trial activated. The fixed calendar starts at the dates printed by prepare.'
echo 'Live Upstox quotes and simulated rotation fills must be checked during the first session.'
runuser -u tradex -- env TRADEX_MODE=upstox-paper "$python" -m execution.paper_trial health
