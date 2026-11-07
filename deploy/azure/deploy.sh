#!/usr/bin/env bash
set -euo pipefail
: "${AZURE_SUBSCRIPTION_ID:?Set subscription ID}"
: "${ADMIN_CIDR:?Set your administrator IPv4 /32 CIDR}"
: "${SSH_PUBLIC_KEY_FILE:?Set public SSH key file path}"
python3 - "$ADMIN_CIDR" "$SSH_PUBLIC_KEY_FILE" <<'PY'
import ipaddress, sys
from pathlib import Path
n = ipaddress.ip_network(sys.argv[1], strict=True)
assert n.version == 4 and n.prefixlen == 32 and n.network_address.is_global, 'Use one public IPv4 /32 administrator address'
key = Path(sys.argv[2]).read_text().strip()
assert len(key.splitlines()) == 1 and key.startswith(('ssh-ed25519 ', 'ssh-rsa ', 'ecdsa-sha2-')), 'Supply a public SSH key file, never a private key'
PY
region=${AZURE_REGION:-centralindia}
group=${AZURE_RESOURCE_GROUP:-tradex-student}
size=${AZURE_VM_SIZE:-Standard_B2ts_v2}
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az group create --name "$group" --location "$region" --output none
template="$(dirname "$0")/main.bicep"
params=(location="$region" vmSize="$size" adminCidr="$ADMIN_CIDR" sshPublicKey="$(cat "$SSH_PUBLIC_KEY_FILE")")
az deployment group validate --resource-group "$group" --template-file "$template" --parameters "${params[@]}"
az deployment group what-if --resource-group "$group" --template-file "$template" --parameters "${params[@]}"
if [[ ${1:-} != --apply ]]; then
  echo 'Validation/what-if only. Inspect the changes and retail costs, then rerun with --apply.'
  exit 0
fi
az deployment group create --resource-group "$group" --template-file "$template" --parameters "${params[@]}"
