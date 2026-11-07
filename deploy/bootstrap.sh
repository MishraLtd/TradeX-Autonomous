#!/usr/bin/env bash
# Run as root from an inspected, versioned checkout on the Azure VM.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
release=${1:?Supply an immutable release ID, preferably the Git commit SHA}
[[ $release =~ ^[A-Za-z0-9._-]+$ ]] || exit 1
root=$(cd "$(dirname "$0")/.." && pwd)
apt-get update
apt-get install -y python3-venv python3-pip ca-certificates
id tradex >/dev/null 2>&1 || useradd --system --home-dir /var/lib/tradex --shell /usr/sbin/nologin tradex
install -d -m 755 /opt/tradex/releases
install -d -m 700 -o tradex -g tradex /var/lib/tradex
install -d -m 750 -o root -g tradex /etc/tradex
[[ ! -e /opt/tradex/releases/$release ]] || { echo 'Release already exists; choose a new version'; exit 1; }
mkdir "/opt/tradex/releases/$release"
tar -C "$root" --exclude=state --exclude=.env --exclude='config.live.json' --exclude='*.sqlite*' --exclude='*.kite-session.json' --exclude='*.cost-profile.json' --exclude=__pycache__ -cf - Phase2A_Execution deploy | tar -C "/opt/tradex/releases/$release" -xf -
python3 -m venv "/opt/tradex/releases/$release/venv"
"/opt/tradex/releases/$release/venv/bin/pip" install -r "/opt/tradex/releases/$release/deploy/requirements-cloud.txt"
chown -R root:root "/opt/tradex/releases/$release"
if systemctl cat tradex.service >/dev/null 2>&1; then
  systemctl stop tradex.service
fi
ln -sfn "/opt/tradex/releases/$release" /opt/tradex/current
install -m 644 "$root"/deploy/systemd/* /etc/systemd/system/
systemctl daemon-reload
echo 'Installed only. Configure worker.env, private config, lease blob and external monitor before enabling services.'
