# Priority 7: cloud deployment and operational controls

The package runs TradeX on one persistent Azure VM without a running Mac. It
includes Azure infrastructure, a managed-identity worker, systemd restart,
independent health publishing, external monitoring, consistent backups, guarded
restore and a conservative daily-loss budget. Manual/API entries are available
for the CNC engine. The new [Upstox intraday paper trial](UPSTOX_PAPER.md) uses
fixed entry times, long and short simulated positions, and real market data.

**Status:** the operator reported successful Azure DEMO paper entry/exit, worker
health, private API and maintenance runs on 9 November 2026. New Upstox trial
deployment and live-feed acceptance remain pending. Delivered notifications,
actual restore drills and broker/account acceptance need their own evidence;
the cloud setup and synthetic tests do not certify live-money trading.

Read [VM choices and measured retail quotes](VM_CHOICES.md) first. Start with
paper mode. Live activation remains a separate decision after account acceptance.

## 1. Validate the subscription and infrastructure

Use Azure Cloud Shell in a browser, or an authenticated Azure CLI. You can do
this from another computer or phone; no Mac process is part of the running system.
Use a public SSH key generated/stored securely for the chosen administrator.
Never paste private keys, broker passwords, OTPs or API secrets into chat/GitHub.

```bash
export AZURE_SUBSCRIPTION_ID='YOUR_SUBSCRIPTION_ID'
export AZURE_REGION=centralindia
export AZURE_RESOURCE_GROUP=tradex-student
export AZURE_VM_SIZE=Standard_B2ts_v2
export ADMIN_CIDR='YOUR_PUBLIC_IPV4/32'
export SSH_PUBLIC_KEY_FILE="$HOME/.ssh/tradex.pub"
bash deploy/azure/preflight.sh
bash deploy/azure/deploy.sh          # validate and what-if; no VM deployment
bash deploy/azure/deploy.sh --apply  # create the inspected resources
```

The resource group itself is created by the validation script. Infrastructure
includes a VM, 32 GiB Standard SSD, static public IPv4, restricted NSG, private
Blob container, Key Vault, managed identity and scoped Blob/secret RBAC. Role
propagation can take time. A tenant that forbids role assignments needs an Azure
administrator to grant these specific roles. No auto-scaling or Spot eviction is
used. Disk retention is deliberate; deleting a VM does not erase trading history.

Create the optional subscription budget with your chosen notification address:

```bash
az deployment sub create --location "$AZURE_REGION" \
  --template-file deploy/azure/budget.bicep \
  --parameters email='YOUR_ALERT_ADDRESS' monthlyLimit=15 startDate='2026-11-01'
```

Use the first day of the actual deployment month. Budget amounts use the
subscription's billing currency. Check the student credit separately; budget
alerts are not an automatic shutdown mechanism and do not extend credit.

## 2. Install an immutable release, initially stopped

SSH into the VM using your restricted administrator key. Fetch an inspected
TradeX commit (use a read-only deploy key if the repository is private). Run:

```bash
sudo bash deploy/bootstrap.sh COMMIT_SHA
```

The installer builds a per-release virtual environment, installs the pinned SDKs,
creates the unprivileged `tradex` service user and units, then switches the release
link with the previous worker stopped. It never starts trading. Releases and
virtual environments are root-owned; mutable state lives in `/var/lib/tradex`.
Keep `/etc/tradex/worker.env` root-owned with group `tradex`, mode 0640:

```text
TRADEX_MODE=paper
TRADEX_ACCOUNT_ID=PAPER-TEST
TRADEX_CONFIG=/var/lib/tradex/config.json
TRADEX_OPERATIONS_URL=https://STORAGE.blob.core.windows.net/operations
TRADEX_LEASE_URL=https://STORAGE.blob.core.windows.net/operations/lease-PAPER-TEST.lock
TRADEX_VAULT_URL=https://VAULT.vault.azure.net
```

Set the real account ID and matching lease blob for a future live deployment.
Copy `config.paper.json` to `/var/lib/tradex/config.json`, add a deliberately
chosen `daily_loss_limit` and optional `daily_loss_action` (`FLATTEN` or `PAUSE`),
then set ownership `tradex:tradex` and permissions 0600. Paper quotes still require
an explicit synthetic feed; an idle process does not simulate real market data.

Create the account lease blob **once**, using an identity with Storage Blob Data
Contributor. Never overwrite/break an existing account lease to force a start:

```bash
printf 'TradeX account ownership anchor\n' > /tmp/tradex-anchor
az storage blob upload --account-name STORAGE --container-name operations \
  --name lease-PAPER-TEST.lock --file /tmp/tradex-anchor --auth-mode login --overwrite false
```

Use the same canonical blob for every worker managing this account. A different
storage account/lease URL does not coordinate ownership. Backups never include
the runtime lease receipt. The cloud worker holds a 60-second renewable Azure
lease and publishes a 35-second local permission receipt; it renews every 15
seconds. Failed or slow renewal removes permission and terminates the child.
Live broker mutations check permission after authentication and immediately
before dispatch, with time reserved for the request. Read-only reconciliation
can continue without mutation permission. A separate lifetime filesystem lock
also rejects a second continuous worker sharing the database.

This is application coordination, not broker-enforced fencing. A request already
in flight remains uncertain. There is **no automatic failover to a cloned VM**.
Before any takeover, stop/isolate the old host, wait for lease expiry and perform
authoritative broker reconciliation; never resume from an old backup alone.

## 3. Enable external monitoring before relying on the service

The VM's managed identity can publish sanitized health JSON and backups to the
private operations container. No trade amounts or credentials enter the external
health file. An independent timer assesses the worker even if its process hangs.

For `.github/workflows/cloud-health.yml`, configure a separate Entra application
or user-assigned identity with GitHub OIDC federation for this repository's main
branch. Grant it **Storage Blob Data Reader** scoped to the operations container;
do not reuse the VM's write identity. Set repository variables:

```text
AZURE_MONITOR_CLIENT_ID
AZURE_TENANT_ID
AZURE_SUBSCRIPTION_ID
AZURE_OPERATIONS_STORAGE
```

No long-lived Azure secret is stored in GitHub. Without the client-ID variable,
the monitoring workflow is explicitly skipped and monitoring is **not active**.
Enable failed Actions notifications for your account, run the workflow manually,
and verify delivery to the intended operator. GitHub scheduled jobs can be delayed
or disabled by platform policy. With the optional VM power schedule, checks run
every five minutes from 07:40 through 16:55 IST on weekdays; planned off-hours
do not create false stale-health failures. This is not a guaranteed alert SLA.
For stricter production alerting, add a separately hosted monitor with
an agreed SLA before live activation. Missing/stale health, unhealthy worker,
authentication failure or subscription unavailability makes the job fail.

```bash
sudo systemctl enable --now tradex.service tradex-health.timer tradex-backup.timer
sudo systemctl status tradex.service
sudo journalctl -u tradex.service -n 30
```

This enables paper mode unless explicitly changed. Test stopping the worker,
missing publication, Azure access rejection and a reboot; confirm the external
monitor actually reports each fault. A successful local unit test is not proof
that cloud notifications have been configured.

## 4. Backups and recovery

The weekday timer runs at 11:00 UTC (16:30 IST), before the optional 17:00 IST
VM deallocation, using SQLite's online backup API;
it captures committed WAL state consistently without copying live WAL files.
Each backup has an account ID, SHA-256 and integrity check. Both DB and manifest
are uploaded create-only, along with parsed configuration/cost-profile snapshots.
Session tokens and API secrets are excluded. Local files are private, retained
seven days; Azure lifecycle retains backup blobs 30 days plus configured soft
delete. LRS is single-region storage. This is not cross-region disaster recovery.
The scheduled backup recovery point is up to 24 hours of engine state; verify last
successful backup regularly. Financial/trading activity after it must be recovered
from authoritative broker evidence before any resumption.

## 5. Weekday VM power schedule

[`azure/vm-schedule.bicep`](azure/vm-schedule.bicep) creates two Consumption
Logic Apps. They start `tradex-worker` at 07:30 IST and deallocate it at 17:00
IST, Monday through Friday. Each workflow's managed identity receives Virtual
Machine Contributor only at that VM's resource scope. No secret, webhook or
public management endpoint is added. Azure manages recurrence time using
`India Standard Time`; systemd restarts the enabled worker and timers after boot.

The public IP in `main.bicep` is static, so deallocation does not change the SSH
address. The OS disk, static public IP, storage, Key Vault and small Logic App
execution charges remain billable while compute is deallocated. The schedule
does not know exchange holidays: on a weekday holiday it still powers on at
07:30 and off at 17:00. Saturday and Sunday stay off after Friday's shutdown.
Manual starts and stops remain available in Azure.

Deploy from an authenticated Cloud Shell checkout after installing the release
that contains the 16:30 backup timer:

```bash
bash deploy/azure/enable-vm-schedule.sh \
  1cc4b1a3-0235-4495-862e-84ed882850c8 \
  tradex-student \
  tradex-worker
```

The deployment is idempotent. The script verifies that both workflows are
enabled and refuses scheduled deallocation if `tradex-ip` is not static. RBAC
can take several minutes to propagate after the first deployment, so do not
force an immediate deallocation during market hours.

```bash
az resource show -g tradex-student \
  --resource-type Microsoft.Logic/workflows \
  -n tradex-vm-start-weekdays \
  --query '{state:properties.state,trigger:properties.definition.triggers}' -o json

az resource show -g tradex-student \
  --resource-type Microsoft.Logic/workflows \
  -n tradex-vm-deallocate-weekdays \
  --query '{state:properties.state,trigger:properties.definition.triggers}' -o json

az network public-ip show -g tradex-student -n tradex-ip \
  --query '{ip:ipAddress,allocation:publicIPAllocationMethod}' -o json
```

After the first scheduled wake, verify the paper worker heartbeat before relying
on unattended operation. After the first scheduled shutdown, the power state
must be `VM deallocated`, which is the compute-saving state. `VM stopped` alone
can remain allocated and billed.

For a drill, download a matching `.sqlite` / `.sqlite.json` pair to an isolated
directory. With the execution service stopped, restore to a **new filename**:

```bash
cd /opt/tradex/current/Phase2A_Execution
/opt/tradex/current/venv/bin/python -m execution.operations restore \
  /PRIVATE/BACKUP.sqlite /PRIVATE/restored.sqlite --account 'kite:YOUR_ACCOUNT'
```

Wrong account, corrupt checksum or an existing destination is rejected. Restore
starts paused, discards stale preflight/heartbeat state, forces polling recovery
and requires financial reconciliation. Never auto-replace the live database or
delete its `-wal` while connected. Preserve the current DB before an operator
chooses a recovered state. Validate order/GTT identities, foreign exposure and
fills since the backup, restore reviewed config, complete preflight and financial
imports, then explicitly resume. An unknown fill is never treated as absent.

## 5. Daily-loss protection

`daily_loss_limit` is a positive rupee budget across the engine-managed account.
The conservative calculation sums losing trades closed today (IST) plus current
open trade liquidation losses, including estimated fees; winning trades do not
replenish it, and carried open losses do not disappear at midnight. Matching
financial results can refine closed-trade costs. Legacy closures with no known
date require financial reconciliation. Stale/missing valuations block entries.

A breach latches durably for that IST day, pauses entries and, by default, records
`DAILY_LOSS_LIMIT` exit requests. Existing GTT cancellation, child-order adoption,
terminal-state checks and residual sizing still govern actual sells. It never
submits a competing sell merely because a loss threshold was crossed. `PAUSE`
can be selected to retain existing exits. Restart/config changes cannot clear
the same-day latch. After a new day and valid valuation, explicit resume is still
required. Health exposes the budget and valuation errors.

This is stricter than a daily net-P&L rule and is not a guaranteed maximum loss:
quotes, fees, polling, price gaps, unavailable sessions, circuits and execution
uncertainty can cause losses beyond the threshold. It covers this engine's
managed trades, not unrelated trading elsewhere in the brokerage account. Use
an isolated execution account and keep the worker running.

## 6. Live transition and daily renewal

After paper/cloud tests and the outstanding real-account acceptance pass:
- Stop the Mac/any previous worker; reconcile its account state before transfer.
- Store `kite-api-key` in the dedicated worker Key Vault. Keep the API secret
  outside this worker-readable vault (operator-only storage or interactive
  login input); the long-running worker only needs the API key and session.
- Install the reviewed live config, calendar and cost profile in private state.
  Set `cloud_lease_file` to `/run/tradex/lease.json` and a daily-loss limit. Select
  `TRADEX_MODE=live`, the exact account ID and its canonical lease anchor.
- Renew the private session through the existing `login-url` / `login` commands
  using an interactive SSH terminal. Browser Azure Cloud Shell can provide that
  terminal from another device, including a phone, with its own restricted SSH
  key and current /32 NSG rule. Your Mac does not have to run.
- Broker authentication remains a daily human task; this package does not
  automate passwords/OTPs or provide a new public web login/control endpoint.
  A browser-only callback UI would require an agreed domain and HTTPS setup.
- Start the worker only after the separately approved live activation. Test
  session expiry, renewal, loss-budget action, backup and external alert delivery.

## 7. Updates and rollback

Stop execution, take a verified backup, inspect the new commit and install it
under a new immutable release ID. `bootstrap.sh` stops the previous service
before changing `current`; it does not automatically start the new one. Keep the
old per-release virtual environment. Roll back code by stopping the worker and
repointing `/opt/tradex/current` to the previous release, then validate schema
compatibility and broker state before restart. **Do not roll back trading history**
to roll back code. Deployment is deliberately explicit, not every GitHub push.

Tests cover WAL backup/restore, wrong-account/corruption rejection, worker locks,
lease-loss behavior, dispatch guards, external stale-health checks and daily-loss
latching through broker-held protection. Infrastructure compilation is checked
in CI. Azure create/validate/what-if, actual restore drills, notification delivery
and a continuous paper soak must pass in the subscription before live use.
