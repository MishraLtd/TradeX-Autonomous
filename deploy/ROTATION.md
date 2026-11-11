# Random stock rotation: local paper only

There are no stock-specific entry times. From 09:15 IST, after calendar and
market-open verification, draw one stock uniformly from the approved stocks
with a fresh live quote, an acceptable spread, displayed liquidity and enough
cash to buy/reserve at least one whole share including entry fees. With eight
eligible stocks each has probability 1/8. On rotation the stock just left is
excluded; with seven alternatives each has probability 1/7. Eligibility may
reduce that pool. These are random selection probabilities, not predictions
of success. If no alternative is eligible, wait for one.

Trade only that focused stock, repeating after each complete exit. The
existing long/short direction formula stays unchanged: instrument index plus
planned-session index even means LONG, odd means SHORT. This formula does not
follow price momentum. One active or pending position is permitted by this
entry policy, within the existing three-position safety ceiling. Each trade
uses up to ₹2,000, including entry fees, and never more than available cash.

After every fully closed trade, calculate realized net P&L as gross P&L minus
all simulated entry and exit fees/taxes. Switch stocks if either:

- Three consecutive trades in the current stock visit have negative net P&L.
- The rolling last three trades in that visit total **₹10 or less net**.

Exactly ₹10 triggers rotation; ₹10.01 does not. Fewer than three trades do not
trigger rotation. Zero net breaks the loss streak. A partial exit does not count
as a completed trade. Three small wins may trigger rotation too. Visiting a
different stock starts fresh counters; a new session starts a fresh draw. Cash
and historical results are never reset. Previously left stocks can be drawn
again after an intervening stock visit.

The existing exits remain: take profit above ₹1 estimated net after fees, stop
on price P&L below −₹2 (fees/taxes additional), and square off at 15:00 IST.
No new entries from 15:00. No daily trade-count cap; the ₹100 accumulated-loss
guard still pauses trading. Fills require a later live quote after latency,
use available simulated liquidity and include adverse slippage. Trigger
thresholds do not guarantee the eventual fill or net result. All orders remain
local simulations; Upstox is used only for data.

## Upgrade an existing VM trial

Run inside the VM as `tradexadmin`, while flat, before 09:10 or after 15:15 IST
on a planned trading day. The VM shuts down at 17:00 on weekdays. Use this
upgrade rather than the fresh-trial installer. It accepts existing three-stock
or eight-stock trials, resolves the approved eight instruments again, backs up
the database/config and preserves balances, fills and the original five dates.

```bash
export TRADEX_RELEASE='FULL_REVIEWED_COMMIT_SHA'
git clone https://github.com/MishraLtd/TradeX-Autonomous.git "$HOME/tradex-rotation-$TRADEX_RELEASE" &&
cd "$HOME/tradex-rotation-$TRADEX_RELEASE" &&
git checkout --detach "$TRADEX_RELEASE" &&
sudo bash deploy/azure/upgrade-upstox-greedy.sh "$TRADEX_RELEASE" --random-rotation
```

The installer checks a fresh worker heartbeat after activation and runs the
health/backup services. It also installs the existing 16:30 backup timer.
Azure's 07:30 start/17:00 deallocation schedules do not need redeployment.

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo systemctl is-active tradex.service tradex-health.timer tradex-backup.timer
sudo -u tradex env TRADEX_MODE=upstox-paper /opt/tradex/current/venv/bin/python -m execution.paper_trial report
```

Confirm `entry_policy` is `RANDOM_ROTATION`, `assumptions.entry_times` is empty,
and `assumptions.instruments` has all eight symbols. `rotation` reports the
selected symbol and current rolling window. Events record `ROTATION_SELECT`
with the candidate set/probability, `ROTATION_RESULT` after a complete close,
and `ROTATION_LEAVE` with its reason. Policy results distinguish scheduled
entries from rotation entries while preserving original fee accounting.

After the next boot, check for `WORKER_STALE`, and during market hours verify
fresh quote updates and simulated fills. The report's `checked_at` is its
generation time, not proof of a worker heartbeat. A successful GitHub push
does not activate this policy on the VM.
