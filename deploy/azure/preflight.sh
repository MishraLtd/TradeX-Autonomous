#!/usr/bin/env bash
set -euo pipefail
: "${AZURE_SUBSCRIPTION_ID:?Set your approved student subscription ID}"
region=${AZURE_REGION:-centralindia}
az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az account show --query '{name:name,id:id,state:state}' -o table
az vm list-usage --location "$region" -o table
az vm list-skus --location "$region" --resource-type virtualMachines --all \
  --query "[?name=='Standard_B2ts_v2' || name=='Standard_B2ls_v2' || name=='Standard_B1s' || name=='Standard_D2s_v5'].{size:name,restrictions:restrictions}" -o json
# A retail quote is not proof of a subscription-specific free entitlement.
echo 'Check remaining student credit and expiry in Education Hub; then run deployment what-if.'
