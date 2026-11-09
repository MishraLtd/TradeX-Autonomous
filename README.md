# TradeX Autonomous

Current focus: **five-session intraday paper trading on real Upstox data**,
with ₹4,000 virtual cash, a ₹2,000 entry cap, at most three positions, fixed
scheduled long/short entries, automatic targets/stops and end-of-day exits.
See the [Upstox paper setup and VM runbook](deploy/UPSTOX_PAPER.md). The trial
uses a read-only Analytics Token and cannot place broker orders. It has its
own frozen config and ledger; live-data/VM acceptance is still pending.

The existing manual/API **long-only CNC** engine is also in
[`Phase2A_Execution`](Phase2A_Execution/README.md). Once it
receives an entry request, it derives a bounded entry price from a fresh quote,
sizes whole shares within the budget, waits for actual fills, calculates a net
profit target and a price-based stop loss, and sells the filled quantity when an
exit triggers. It registers paired target/stop GTT triggers with the broker so
verified protection can operate while the worker is offline. Users configure
percentages and limits once instead of entering prices for every trade.

## Run the paper demonstration

Python 3.11+ on Linux/macOS; no dependencies needed for paper mode:

```bash
cd Phase2A_Execution
python demo.py
python demo_recovery.py
python -m unittest discover -s tests -v
```

The demo uses temporary databases and synthetic `NSE:DEMO` quotes. The paper
broker completes a profit-target exit and a stop-loss exit while the engine is
stopped; the engine then restarts and reconciles each fill. It makes no live calls.
The recovery demo shows a stop-limit order stuck after a price gap, followed by
deadline-based cancellation, confirmed residual sizing, and a replacement exit.

## Included

- Exact exchange/symbol whitelist, per-trade and total capital limits.
- IOC limit entries sized from budget and a fresh quote; confirmed-fill pricing.
- Automatic fee-estimated profit targets and tick-rounded stop levels.
- Broker-held OCO/GTT protection, verified after registration and required in live mode.
- One active exit order per position, partial-fill accounting, bounded retries.
- GTT child-order recovery and cancellation checks to prevent competing sells.
- Persisted exit/cancellation deadlines, spaced retries, and critical escalation.
- Explicit emergency-exit policy when the local quote feed is unavailable.
- SQLite intent journal, reservations, account binding and process locking.
- Restart reconciliation, duplicate request protection and ambiguous-order handling.
- Pause/resume, flatten, status, JSON journal, and execution health beyond heartbeat.
- Persistent paper broker and optional explicitly enabled Kite CNC equity adapter.
- Read-only Kite account acceptance reports, explicit account binding and corrected delivery-inventory reconciliation.
- Supported Kite login, private renewable session receipts, and expiry/revocation alerts.
- Reviewed NSE/BSE execution calendars, pre-market validation and next-day recovery guards.
- Batched validated market data, measured polling cadence, reconnect reconciliation and exit-response deadlines.
- Account-bound cost profiles, split-exit fee allowances, and audited reconciliation of fills, holdings, cash and net P&L.
- Azure deployment templates, supervised worker, account lease checks, consistent backup/paused restore and conservative daily-loss controls.
- Lifecycle/recovery tests and GitHub Actions checks.

## Status and boundaries

Paper mode is the default. The Kite adapter is implemented and tested with mocked
API responses and the installed SDK; no live account/order validation has been
performed. It requires your approved symbols, fee assumptions, broker credentials
and deployment setup. The [account acceptance guide](Phase2A_Execution/ACCOUNT_ACCEPTANCE.md)
provides a GET-only validation command; real-account evidence is still pending.
The [session and market-hours guide](Phase2A_Execution/SESSIONS_AND_MARKETS.md)
documents daily broker login and calendar maintenance. Bundled calendar coverage
ends on 31 December 2026; unverified dates block new order actions.
The [market-data guide](Phase2A_Execution/MARKET_DATA_AND_LATENCY.md) documents the
60-second monitored exit-response deadline and synthetic timing acceptance;
actual broker latency and account validation are still pending.
The [costs and reconciliation guide](Phase2A_Execution/COSTS_AND_RECONCILIATION.md)
documents the new live-entry cost-profile requirement and cumulative statement
imports. Missing prior-day financial evidence or discrepancies block new entries
while exits continue. Real account tariff/contract-note validation remains pending.
The [cloud deployment guide](deploy/README.md) and [VM comparison](deploy/VM_CHOICES.md)
cover Azure student credit, secure installation, external monitoring and recovery.
The operator has reported a successful Azure DEMO paper deployment. Upstox
real-data trial acceptance, restore drills and delivered notifications remain pending.
Protection starts after the entry is terminal and the GTT
has been registered and verified; the fill-to-registration window is unprotected.
GTTs are broker-held triggers that submit limit orders, not guaranteed exits:
a gap through the limit can leave an order unfilled, and the sibling trigger is
consumed when either leg fires. The worker now cancels overdue sells and attempts
bounded residual exits after cancellation is confirmed. Broker failures or market
restrictions can still require intervention; the health command exposes those
conditions. Recovery still needs a running worker. See the
[operating guide](Phase2A_Execution/README.md) before using the live adapter.

Trusted Python integrations can also call the underlying engine interface:

```python
engine.request_entry(request_id="unique-stable-id", stock="NSE:YOUR_SYMBOL", budget="1000")
```

No prediction/training pipeline is required for this execution-only workflow.
The existing Phase 2 models, cost/P&L library and Phase 3 portfolio components
remain available as separate research/integration components. Their previous
status is recorded in [the extraction README](docs/EXTRACTION_README.md).

## Controlled entry

The engine supports operator-triggered buys through the manual CLI and a private,
authenticated HTTP API restricted to the configured stock universe. See the
[controlled entry guide](Phase2A_Execution/CONTROLLED_ENTRY.md) for setup, replay
semantics, worker requirements and Azure service instructions.
