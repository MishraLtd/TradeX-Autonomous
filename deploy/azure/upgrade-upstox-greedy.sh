#!/usr/bin/env bash
# Existing Upstox paper trial only. Run after 15:15 / before 09:10 IST, flat.
set -euo pipefail
umask 077
[[ $EUID -eq 0 ]] || { echo 'Run with sudo on the Azure VM'; exit 1; }
root=$(cd "$(dirname "$0")/../.." && pwd)
release=${1:?Supply the full reviewed Git commit SHA}
expand=${2:-}
[[ -z $expand || $expand == --expand-universe ]] || { echo 'Unknown upgrade option'; exit 1; }
[[ $release =~ ^[a-f0-9]{40}$ ]] || { echo 'A full Git SHA is required'; exit 1; }
export GIT_OPTIONAL_LOCKS=0
[[ $(git -C "$root" rev-parse HEAD) == "$release" ]] || { echo 'Checkout does not match release'; exit 1; }
[[ -z $(git -C "$root" status --porcelain --untracked-files=normal) ]] || { echo 'Use a clean pinned checkout'; exit 1; }
set -a
source /etc/tradex/worker.env
set +a
[[ ${TRADEX_MODE:-} == upstox-paper ]] || { echo 'Only an existing Upstox paper trial can be upgraded'; exit 1; }
config=${TRADEX_TRIAL_CONFIG:-/var/lib/tradex/upstox-trial/config.json}
db=${TRADEX_TRIAL_DB:-/var/lib/tradex/upstox-trial/trial.sqlite}
[[ -f $config && -f $db ]] || { echo 'Existing config and ledger are required'; exit 1; }
previous=$(readlink -f /opt/tradex/current)
candidate="/opt/tradex/releases/$release"
[[ $candidate != "$previous" ]] || { echo 'Release already selected; inspect status before repeating an upgrade'; exit 1; }
source_fingerprint=$(cd "$previous/Phase2A_Execution" && "$previous/venv/bin/python" -c \
  'from execution.paper_trial.config import implementation_fingerprint; print(implementation_fingerprint())')
if [[ ! -d $candidate ]]; then
  bash "$root/deploy/bootstrap.sh" "$release" --stage-only
fi
python="$candidate/venv/bin/python"
cd "$candidate/Phase2A_Execution"
"$python" - "$root/Phase2A_Execution/execution/paper_trial" <<'PY'
import hashlib, sys
from pathlib import Path
from execution.paper_trial.config import implementation_fingerprint
h = hashlib.sha256()
for path in sorted(Path(sys.argv[1]).iterdir()):
    if path.suffix in ('.py', '.proto'):
        h.update(path.name.encode() + b'\0' + path.read_bytes())
if h.hexdigest() != implementation_fingerprint():
    raise SystemExit('Staged code differs from the reviewed checkout; preserve and inspect that release directory')
PY
# Only authorization is checked; no order endpoint or signed URL is printed.
runuser -u tradex -- env TRADEX_MODE=upstox-paper TRADEX_VAULT_URL="${TRADEX_VAULT_URL:?}" \
  "$python" -c 'from execution.paper_trial.upstox import DataClient, analytics_token; DataClient(analytics_token()).websocket_url(); print("Data authorization accepted")'
backup="$(dirname "$db")/policy-upgrades/$release"
upgrade=("$python" -m execution.paper_trial --config "$config" --db "$db" upgrade-greedy
  --backup-directory "$backup" --expected-implementation "$source_fingerprint" --release "$release")
if [[ $expand == --expand-universe ]]; then upgrade+=(--expand-universe); fi
migration_started=0
failed() {
  if [[ $migration_started -eq 0 ]]; then
    systemctl start tradex.service tradex-health.timer tradex-backup.timer || true
    echo 'Upgrade check failed; the previous trial is unchanged and its services were restarted.' >&2
  else
    systemctl stop tradex.service || true
    echo "Upgrade did not complete. Worker remains stopped; preserve $backup and inspect config/ledger hashes." >&2
    echo 'Do not run the fresh-trial installer or reset the database. See deploy/UPSTOX_GREEDY.md.' >&2
  fi
}
trap failed ERR
systemctl stop tradex-health.timer tradex-backup.timer
systemctl stop tradex-maintain@health.service tradex-maintain@backup.service tradex.service
"${upgrade[@]}" --check
migration_started=1
"${upgrade[@]}"
printf '%s\n' "$previous" > "$backup/previous-release"
# SQLite sidecars/lock may have been created by this offline root migration.
for path in "$db" "$db-wal" "$db-shm" "$db.lock"; do
  if [[ -f $path ]]; then chown tradex:tradex "$path"; chmod 600 "$path"; fi
done
chown root:tradex "$config"
chmod 640 "$config"
ln -sfn "$candidate" /opt/tradex/current
install -m 644 "$candidate"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
activation=$("$python" -c 'import time; print(time.time())')
systemctl start tradex.service
ready=0
for attempt in {1..45}; do
  if "$python" - "$db" "$config" "$activation" <<'PY'
import sys
from execution.paper_trial.config import TrialConfig, implementation_fingerprint
from execution.paper_trial.ledger import read_state
s = read_state(sys.argv[1])
ok = (s['heartbeat'] >= float(sys.argv[3]) and s['config_hash'] == TrialConfig.load(sys.argv[2]).fingerprint
      and s['implementation_sha256'] == implementation_fingerprint())
raise SystemExit(0 if ok else 1)
PY
  then ready=1; break; fi
  sleep 1
done
[[ $ready -eq 1 ]] || { echo 'No fresh heartbeat from the upgraded worker'; false; }
systemctl start tradex-maintain@backup.service
systemctl start tradex-maintain@health.service
systemctl start tradex-health.timer tradex-backup.timer
trap - ERR
echo 'Reviewed paper release active. Cash, policy history, prior trades and original sessions were preserved.'
runuser -u tradex -- env TRADEX_MODE=upstox-paper "$python" -m execution.paper_trial \
  --config "$config" --db "$db" status
