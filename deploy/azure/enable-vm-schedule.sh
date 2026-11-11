#!/usr/bin/env bash
# Run from an authenticated Azure Cloud Shell checkout.
set -euo pipefail

subscription=${1:?Usage: enable-vm-schedule.sh SUBSCRIPTION_ID [RESOURCE_GROUP] [VM_NAME]}
resource_group=${2:-tradex-student}
vm_name=${3:-tradex-worker}
root=$(cd "$(dirname "$0")/../.." && pwd)

[[ $subscription =~ ^[0-9a-fA-F-]{36}$ ]] || { echo 'Subscription must be a UUID'; exit 1; }
[[ $resource_group =~ ^[-A-Za-z0-9._()]+$ ]] || { echo 'Invalid resource group'; exit 1; }
[[ $vm_name =~ ^[-A-Za-z0-9._]+$ ]] || { echo 'Invalid VM name'; exit 1; }

az account set --subscription "$subscription"
az vm show --resource-group "$resource_group" --name "$vm_name" --query id --output tsv >/dev/null

for provider in Microsoft.Logic Microsoft.ManagedIdentity; do
  az provider register --namespace "$provider" --wait --output none
done

az deployment group create \
  --resource-group "$resource_group" \
  --name tradex-vm-power-schedule \
  --template-file "$root/deploy/azure/vm-schedule.bicep" \
  --parameters vmName="$vm_name" \
  --query 'properties.outputs' \
  --output json

for workflow in tradex-vm-start-weekdays tradex-vm-deallocate-weekdays; do
  state=$(az resource show --resource-group "$resource_group" \
    --resource-type Microsoft.Logic/workflows --name "$workflow" \
    --query properties.state --output tsv)
  [[ $state == Enabled ]] || { echo "$workflow is not enabled"; exit 1; }
done

allocation=$(az network public-ip show --resource-group "$resource_group" \
  --name tradex-ip --query publicIPAllocationMethod --output tsv)
[[ $allocation == Static ]] || { echo 'Refusing scheduled deallocation: public IP is not static'; exit 1; }

echo 'Enabled: Monday-Friday start 07:30 and deallocate 17:00 Asia/Kolkata.'
echo 'The public IP is static. Managed disks and the public IP remain billable while the VM is off.'
