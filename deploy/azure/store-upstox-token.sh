#!/usr/bin/env bash
# Run in Azure Cloud Shell. No token in arguments, output, Git or shell history.
set -euo pipefail
set +x
umask 077
vault=${1:?Usage: bash deploy/azure/store-upstox-token.sh YOUR_VAULT_NAME}
command -v az >/dev/null || { echo 'Run this in authenticated Azure Cloud Shell'; exit 1; }
temporary=$(mktemp)
trap 'rm -f "$temporary"; unset tradex_analytics_token' EXIT
read -r -s -p 'Paste the Upstox ANALYTICS token (hidden): ' tradex_analytics_token
printf '\n'
[[ ${#tradex_analytics_token} -ge 20 && ${#tradex_analytics_token} -le 8192 ]] || { echo 'Invalid token length'; exit 1; }
printf '%s' "$tradex_analytics_token" > "$temporary"
unset tradex_analytics_token
az keyvault secret set --vault-name "$vault" --name upstox-analytics-token \
  --file "$temporary" --encoding utf-8 --output none
echo 'Analytics token saved privately in Key Vault.'
