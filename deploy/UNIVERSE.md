# Eight-stock paper universe

The approved list adds five stocks to the existing three. Upstox NSE instrument
and MIS files were checked on 12 November 2026; the installer resolves them
again and refuses the change if any is missing, ambiguous or not MIS eligible.
The worker repeats that verification each session. Keys and ticks are taken
from the provider, not copied from a company-name guess.

| Stock | Upstox symbol | First entry IST |
| --- | --- | --- |
| State Bank of India | SBIN | 09:30 |
| ITC | ITC | 10:30 |
| HDFC Bank | HDFCBANK | 11:30 |
| Kilburn Engineering | KLBRENG-B | 12:00 |
| Adani Enterprises | ADANIENT | 12:30 |
| Bajaj Finance | BAJFINANCE | 13:00 |
| One97 Communications | PAYTM | 13:30 |
| IndusInd Bank | INDUSINDBK | 14:00 |

The existing alternating direction formula extends to the new slots. On session
3 (13 November for the existing 11/12/13/16/17 November calendar), directions
in table order are LONG, SHORT, LONG, SHORT, LONG, SHORT, LONG, SHORT.
These are fixed operational test entries, not recommendations.

The maximum remains three open/pending positions, with the original cash balance,
₹2,000 per-trade cap, ₹100 daily loss guard and 15:00 square-off. Each first-entry
window remains 60 seconds. A stock may be skipped for insufficient cash, an
unaffordable share, full position capacity, stale data or risk pause. Earlier
stocks may still be recycling when later slots arrive; eight approved stocks
does not guarantee eight entries. No positions are forcibly closed to make room.
Profit remains above ₹1 after fees; the stop remains more than ₹2 price loss
with fees additional. Paper only; no real orders.

## Upgrade on the VM

Use a new checkout pinned to the reviewed release. Run after 15:15 IST while
flat and before the 17:00 VM shutdown, or before 09:10 the next planned session.

```bash
export TRADEX_RELEASE='FULL_REVIEWED_COMMIT_SHA'
git clone https://github.com/MishraLtd/TradeX-Autonomous.git "$HOME/tradex-universe-$TRADEX_RELEASE" &&
cd "$HOME/tradex-universe-$TRADEX_RELEASE" &&
git checkout --detach "$TRADEX_RELEASE" &&
sudo bash deploy/azure/upgrade-upstox-greedy.sh "$TRADEX_RELEASE" --expand-universe
```

This supports original, prior NET_CASH and already-corrected policy releases.
It takes a backup, preserves cash/fills and the original trial dates, records
the universe change, and activates it on the next session that has not begun.
Past days do not acquire five false missed entry slots. The installer also
installs the 16:30 backup timer. Azure power schedules do not need redeployment.

Verify the printed `UNIVERSE_UPGRADED`, `universe_effective_date`, all eight
instruments and entry times. Then check the running worker and report:

```bash
cd /opt/tradex/current/Phase2A_Execution
sudo systemctl is-active tradex.service tradex-health.timer tradex-backup.timer
sudo -u tradex env TRADEX_MODE=upstox-paper /opt/tradex/current/venv/bin/python -m execution.paper_trial report
```

An enabled morning start schedule is not proof of successful boot or a live feed.
Confirm heartbeat and Upstox quote updates after the next startup. The report's
`checked_at` is report-generation time, not the worker heartbeat; inspect
`WORKER_STALE` in its health codes instead of using checked_at as worker age.
