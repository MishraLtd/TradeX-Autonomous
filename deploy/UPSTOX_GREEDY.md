# Upgrade an existing paper trial to net profit and price-loss exits

This change is confined to `execution.paper_trial`: Upstox supplies read-only
market data and every order/fill remains local simulation. It does not enable
the legacy live broker engine or place orders in an Upstox account.

## Policy

| Item | Behavior |
| --- | --- |
| Profit trigger | Estimated net liquidation P&L **> ₹1** for the whole trade |
| Loss trigger | Estimated price P&L **< −₹2** for the whole trade; fees/taxes are additional |
| Exactly ₹1 net / −₹2 price P&L | No threshold exit yet; comparisons are strict |
| Net calculation | Realized partial-exit gross + remaining shares marked at simulated executable bid/ask − paid entry fees − paid exit fees − estimated remaining exit fees |
| Price P&L calculation | Realized partial-exit gross + remaining shares marked at simulated executable bid/ask; no fee/tax deduction for this stop comparison |
| Fill assumptions | Bid/ask spread, adverse 5 bps slippage and tick rounding; at least 250 ms and a later live quote; 10% displayed depth per frame |
| Initial entry | Existing SBIN 09:30, ITC 10:30, HDFCBANK 11:30 IST slots, existing alternating daily directions and sizing |
| Re-entry | Once fully closed by a profit/loss trigger, enter the same stock in the same direction on a later live quote; no prediction or direction reversal |
| Count limit | No daily trade-count cap; unique persisted IDs link each repeat to its parent |
| Other limits | ₹4,000 original capital, ₹2,000 per-entry budget including fees, max 3 simultaneous/pending positions, no leverage, same ₹100 daily losing-trade loss guard |
| Intraday end | Re-entry stops and remaining positions receive square-off requests at **15:00 IST**; 15:10 unresolved-position deadline remains |

The existing intraday cutoff is retained, so this does not open new positions
until 15:30 or during the closing auction. A missed initial schedule slot never
starts recycling. Partial exits reserve their remaining position/capital;
re-entry waits until the entire position closes. Disconnected/stale/snapshot
quotes cannot fill. Pending repeats expire after 60 seconds and may be retried
while the original chain remains eligible. Risk pauses, square-off exits and
unresolved incidents never restart a chain. First scheduled entries have
priority over new repeat intents in each processing step.

**The ₹2 stop is price loss before fees and taxes.** With ₹0.60 total estimated
fees/taxes and a ₹2 price loss, net loss is ₹2.60; the strict stop requests an
exit when the price loss exceeds ₹2. Fees/taxes alone cannot trigger this stop.
For one share bought at ₹234, the price threshold is below ₹232; for multiple
shares it remains ₹2 across the whole position, not ₹2 per share. Executable
prices still reflect spread, adverse slippage and tick rounding, which can
create price loss even without a change in the last traded price.

Profit-taking still requires net profit above ₹1 after all fees and taxes. Cash,
net reports and the unchanged ₹100 daily loss guard continue to include those
charges. An accepted exit remains pending through partial fills; price movement
during latency means final profit may be below ₹1 or price loss may exceed ₹2.
There is no added cooldown and neither threshold guarantees a final fill price.

## Estimated charges

Stamp duty was already present in the old total. Reports now expose it and
every component on both legs. The current model uses the explicitly dated
March 2026 equity-intraday table on the
[official Upstox charges page](https://upstox.com/brokerage-charges/), reviewed
11 November 2026. That page also contains an older repeated table; the new
model uses the later dated schedule.

| Component | Calculation |
| --- | --- |
| Brokerage | min(₹20, 0.1% of order turnover), each side |
| NSE transaction | 0.00307% of turnover, each side; revised combined transaction/IPFT structure |
| Residual IPFT | ₹0.01 per crore, each side, as listed in the current table |
| SEBI | ₹10 per crore, each side |
| GST | 18% of brokerage + transaction + IPFT |
| STT | 0.025% of sell turnover |
| Stamp duty | 0.003% of buy turnover, including short-cover buys |
| DP | Zero for this intraday simulation |

Each simulated order's aggregate estimate rounds to paise. Components retain
precision and include an explicit `rounding_adjustment` so they reconcile to
the charged total. Partial exit fills charge only the increase in cumulative
fees, retaining the single-order brokerage cap. Each re-entry is a new order
with new fees. Actual contract-note aggregation, statutory rounding and account
plans may differ. Delivery, auctions, broker-assisted square-off penalties,
financing, platform subscriptions and account maintenance are not simulated.
Spread and slippage are reflected in fill prices and reported separately;
they are not subtracted a second time as fees.

Historical fees remain under their original model. `policy_results` separates
old fixed exits, the earlier NET_CASH policy and corrected NET_PROFIT_PRICE_STOP
exits; `policy_history` records the effective
change time, release, old/new configs and cash at the boundary. This is a changed
experiment, not five sessions of one unchanged policy.

## Apply on the VM

Run while flat, **after 15:15 IST or before 09:10 IST**, with at least one planned
session remaining. Use the reviewed full SHA from the GitHub commit link:

```bash
export TRADEX_RELEASE='FULL_REVIEWED_COMMIT_SHA'
git clone https://github.com/MishraLtd/TradeX-Autonomous.git "$HOME/tradex-greedy-$TRADEX_RELEASE"
cd "$HOME/tradex-greedy-$TRADEX_RELEASE"
git checkout --detach "$TRADEX_RELEASE"
sudo bash deploy/azure/upgrade-upstox-greedy.sh "$TRADEX_RELEASE"
```

This upgrade accepts either the original fixed-exit release or `41ce136`'s
NET_CASH release. It appends to any existing policy history. Use a new checkout;
do not modify `/opt/tradex/current` or reuse the original
fresh-trial installer. The upgrade:

1. Stages the pinned release and checks read-only data authorization before
   stopping services.
2. Stops the worker and maintenance timers; verifies the source config/code
   hashes, a flat account, no pending entries/incident and the permitted time.
3. Creates a SQLite snapshot, config copy and migration receipt under
   `/var/lib/tradex/upstox-trial/policy-upgrades/<release>/`. It preserves all
   trades, events, cash, fees, sessions, quotes and existing daily reports.
4. Updates only the policy/config/source identity and appends an audit event.
   It does not replenish ₹4,000 or restart the five-session calendar.
5. Selects the new release, restores worker ownership, starts the worker, waits
   for a new heartbeat, uploads a backup and runs health maintenance.

The expected post-day-one balance remains ₹3,968.42 **if no further trading has
occurred**. The installer prints the actual preserved balance. It cannot verify
future live-feed behavior outside market hours.

## Verify and inspect

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo systemctl is-active tradex.service tradex-health.timer tradex-backup.timer
sudo -u tradex env TRADEX_MODE=upstox-paper \
  /opt/tradex/current/venv/bin/python -m execution.paper_trial report
```

Confirm `exit_policy=NET_PROFIT_PRICE_STOP`, `net_profit_inr=1`,
`price_loss_inr=2`, `loss_basis=PRICE`, `net_loss_inr=null`,
`recycle_entries=true`, prior closed trades and original session dates. New
trades have null `target`/`stop`, `NET_PROFIT_TAKE` or `PRICE_LOSS_LIMIT` exit reasons,
and repeated IDs such as `2026-11-12:0:r1`. Inspect `net_pnl_estimate`, `gross_pnl_estimate`,
`entry_fee_components`, `estimated_exit_fee_components`, `paid_fee_components`,
`policy_results`, `policy_history` and fill timestamps. At market hours also
verify fresh quotes, increasing update counts and `market.normal=true`.

## If activation fails

A failure before migration restarts the unchanged worker. A failure after
migration begins leaves the worker stopped and preserves all evidence; it does
not automatically restore an old snapshot over potentially newer fills.

Inspect `sudo journalctl -u tradex.service -n 60 --no-pager` and the private
upgrade directory. If config, ledger and selected release all match the new
fingerprints, correct the reported operational failure and start the worker,
then run backup/health maintenance and timers. Do not rerun the one-time
migration against an already migrated database.

A power interruption between the config-file replacement and SQL commit can
leave a config/ledger mismatch. The worker refuses that pair. Preserve the
current DB/config before repair and compare their hashes with `upgrade.json`.
The database's embedded config identifies which transaction committed; restore
the matching config and release only after inspecting that evidence. Never
delete a ledger, rewrite balances or change fingerprints to bypass this check.
An isolated rollback uses the complete pre-upgrade DB **and** config with its
original release, while preserving the new state separately for audit.
