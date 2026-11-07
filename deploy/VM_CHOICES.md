# Azure student VM choices for TradeX

Checked 8 November 2026 (India time). Recommendation: start the paper deployment
on **Standard_B2ts_v2**, then use **Standard_B2ls_v2** if measured memory pressure
or CPU-credit depletion justifies the extra cost. Keep execution and small-scale
monitoring on this VM; run builds/tests on GitHub Actions.

Central India Linux PAYG compute quotes below come from Microsoft's public
Retail Prices API. The complete meter IDs, effective dates and collected quote
are in [retail-quotes-centralindia.json](azure/retail-quotes-centralindia.json).
Reproduce with `python deploy/azure/quote_vms.py centralindia`. Quotes exclude
Windows, Spot, Low Priority and similarly named Cloud Services meters.

| SKU | vCPU | RAM | CPU model | USD/hour | USD/730 hours | Compute-only $100 runway | Fit |
|---|---:|---:|---|---:|---:|---:|---|
| B1s | 1 | 1 GiB | Burstable, older family | .0112 | 8.18 | 12.2 months* | Fallback if newer sizes unavailable. |
| **B2ts_v2** | **2** | **1 GiB** | Burstable, 20% VM baseline | **.0112** | **8.18** | **12.2 months*** | Lowest-cost recommended starting point; tight memory. |
| **B2ls_v2** | **2** | **4 GiB** | Burstable, 30% VM baseline | **.0448** | **32.70** | **3.1 months** | Comfortable execution host, more RAM for SDK/maintenance. |
| B2s | 2 | 4 GiB | Burstable, older family | .0448 | 32.70 | 3.1 months | Alternative when v2 unavailable; inspect regional quota. |
| B2s_v2 | 2 | 8 GiB | Burstable, 40% VM baseline | .0896 | 65.41 | 1.5 months | Extra RAM currently unnecessary for the execution engine. |
| D2s_v5 | 2 | 8 GiB | Sustained general-purpose CPU | .1010 | 73.73 | 1.4 months | Upgrade for sustained workloads after measurement. |

*Credit expires according to the offer: this calculation does not extend its
validity. Actual runway is shorter after disk, public IPv4, storage transactions,
backup retention, Key Vault and any other resources/taxes are included. Promotional
free allocations can lower costs only if your subscription actually qualifies.
Azure for Students credit is $100, not $100/month. A full year of 24/7 operation
has a credit-only average budget of $8.33/month; even the cheapest listed VM
leaves almost nothing for its other resources. Do not promise a free full year
without checking Education Hub's exact entitlements, balance and expiry.

B-series earns CPU credits below baseline and throttles when credits run out.
Watch CPU Credits Remaining, CPU usage, memory, swap, disk space, poll-cycle
duration and exit-response alerts. Baseline percentages are not benchmark scores.
All listed options are x64; v2 B sizes have no local temporary disk. The Bsv2
published network ceiling is 6,250 Mbps and the small sizes' remote Premium SSD
ceiling is 3,750 IOPS / 85 MB/s; these are platform ceilings, not performance
delivered by the proposed Standard SSD. D2s_v5 provides 2 vCPU / 8 GiB without
B-series CPU-credit dependence. A faster VM cannot bypass Kite rate limits,
market liquidity, exchange circuits or broker latency.

## Proposed configuration

| Component | Selection |
|---|---|
| Region | Central India first; check subscription restrictions and actual broker latency before selecting another region. |
| OS | Ubuntu Server 24.04 LTS, x64, Gen2 image selection verified by Azure deployment validation. |
| Default size | Standard_B2ts_v2; B2ls_v2 selectable through the same template. |
| Storage | 32 GiB persistent Standard SSD OS disk, SQLite WAL with FULL synchronization, retained disk on VM deletion. Never store state on temporary disk. |
| Runtime | Python virtual environment per immutable release; one execution process, systemd automatic restart. |
| Networking | Static Standard public IPv4, SSH public-key authentication from one public administrator /32; no public trading API or database port. |
| Secrets | Managed identity + Key Vault; session file mode 0600, service account and restrictive systemd permissions. |
| Backup | Consistent SQLite online backup nightly, private LRS Blob Storage, 30-day cloud / 7-day local retention. |
| Monitoring | Independent one-minute health publisher and external GitHub Actions checks; failure notifications must be enabled and tested. |
| CPU/RAM budget | No ML training, builds, graphical desktop or large dashboard on the execution VM. Default service memory ceiling 650 MiB; raise it on a larger VM based on measurement. |
| Availability | One VM, no automatic multi-host failover. Reboot/recovery supported; not high availability. |

## Credit and performance decisions

1. Check region/SKU restrictions and family vCPU quota with `azure/preflight.sh`.
2. Check **your** remaining credit/expiry and free-service meter eligibility in
   Azure Education Hub; this environment has no subscription access.
3. Quote all resources in the Azure calculator and inspect deployment what-if.
   Set budget notifications at 50%, 80% and forecasted 100%; these are alerts,
   not spending caps or guarantees against credit exhaustion.
4. Run paper mode continuously, including maintenance and reboot tests. If the
   small VM swaps persistently, OOMs, exhausts CPU credits or misses polling
   budgets, use B2ls_v2. Increasing RAM without measured pressure does not speed
   broker execution. Leave trading timeouts/rate limits intact.
5. Keep the subscription spending limit. Credit depletion can stop the worker;
   broker protection still needs its own acceptance testing. Do not automatically
   stop/deallocate the VM overnight while orders or holdings need supervision.

Official references:
- [Bsv2 specifications and credit behavior](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bsv2-series)
- [Bv1 specifications](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/bv1-series)
- [Dsv5 specifications](https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series)
- [Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices)
- [Student offer and spending limits](https://azure.microsoft.com/en-us/pricing/offers/ms-azr-0170p/)
