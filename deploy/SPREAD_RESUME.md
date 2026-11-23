# Bounded NSE/BSE paper-trial resumption

The November 2026 resumption is configured for **24–27 November inclusive,
Asia/Kolkata**. November 24 is an NSE equity holiday (NSE/CMTR/71775).
The receiver verifies both exchange calendars each session; expected trading
days are November 25–27. It also requires both market segments NORMAL_OPEN.

From a clean checkout pinned to the reviewed release, on the VM after 15:15
or before 09:10 IST, run:

```bash
sudo bash deploy/azure/upgrade-upstox-greedy.sh FULL_RELEASE_SHA \
  --cross-exchange-window 2026-11-24 2026-11-27
```

The existing migration checks the source fingerprint, flat portfolio and no
pending entries. It snapshots the ledger/config, preserves cash, prior fills,
fees, sessions and history, and updates their fingerprints together. It stages
a versioned release and verifies a fresh worker heartbeat. Do not edit the
active release, reset the ledger or use the fresh-trial installer.

The explicit window blocks new entries before its start and after its end;
there is no automatic return to trend trading. Existing 09:30 entry warmup,
15:00 flatten cutoff and 15:10 hard close remain. The trial marks itself
completed at the final hard close, while the worker can continue publishing
health. An existing `entries.paused` operator marker remains respected.

Verify using `python -m execution.paper_trial status` under the existing
`tradex` service account and `TRADEX_MODE=upstox-paper`. Check `spread_window`,
`entry_schedule_status`, `release`, `heartbeat` and `market.entries_paused`.
Before the start, entry policy reports `PAUSED_OUTSIDE_SPREAD_WINDOW`; inside
the window it reports `CROSS_EXCHANGE`. The calendar/market checks still gate
actual entries. `spread_session.day` may retain the last tested day until the
next spread session starts; its historical counts are not reset by migration.

## Rejection and freshness evidence

`ENTRY_SKIPPED` retains its broad reason for compatibility and now includes
`diagnostics.detail`, venue prices/quantities, book/receipt/last-trade ages,
elapsed request time, and configured latency. Details distinguish lost raw
spread, slippage/tick effects, insufficient depth/cash, non-positive net after
fees, previously consumed depth, and waits for fresh paired books. Expiries
record the last observed wait cause where available; none of this reconstructs
missing historical evidence from Friday.

`stale_quotes` identifies active monitored symbols and the timestamps that
fail freshness. Unmonitored historical BSE quotes do not cause the current
NSE-only alert. No quote-age, synchronization, slippage or positive-net guard
has been relaxed to manufacture fills. A stale individual symbol can remain
untradeable until genuinely fresh market data arrives.

The retained fee model is explicitly the existing Zerodha intraday estimate;
Upstox is the data provider. This change does not claim it models Upstox broker
charges. Fills remain local simultaneous-pair simulations, with no broker
order transport and no real money.
