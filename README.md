# TradeX Autonomous

Current focus: **execute and manage long-only trades inside an approved stock list**.
The rule deciding *when to buy* remains a separate, deliberately unfinished input.

The runnable engine is in [`Phase2A_Execution`](Phase2A_Execution/README.md). Once it
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
python -m unittest discover -s tests -v
```

The demo uses temporary databases and synthetic `NSE:DEMO` quotes. The paper
broker completes a profit-target exit and a stop-loss exit while the engine is
stopped; the engine then restarts and reconciles each fill. It makes no live calls.

## Included

- Exact exchange/symbol whitelist, per-trade and total capital limits.
- IOC limit entries sized from budget and a fresh quote; confirmed-fill pricing.
- Automatic fee-estimated profit targets and tick-rounded stop levels.
- Broker-held OCO/GTT protection, verified after registration and required in live mode.
- One active exit order per position, partial-fill accounting, bounded retries.
- GTT child-order recovery and cancellation checks to prevent competing sells.
- SQLite intent journal, reservations, account binding and process locking.
- Restart reconciliation, duplicate request protection and ambiguous-order handling.
- Pause/resume, flatten, status, JSON journal, polling worker and heartbeat.
- Persistent paper broker and optional explicitly enabled Kite CNC equity adapter.
- Lifecycle/recovery tests and GitHub Actions checks.

## Status and boundaries

Paper mode is the default. The Kite adapter is implemented and tested with mocked
API responses and the installed SDK; no live account/order validation has been
performed. It requires your approved symbols, fee assumptions, broker credentials
and deployment setup. Protection starts after the entry is terminal and the GTT
has been registered and verified; the fill-to-registration window is unprotected.
GTTs are broker-held triggers that submit limit orders, not guaranteed exits:
a gap through the limit can leave an order unfilled, and the sibling trigger is
consumed when either leg fires. Recovery still needs a running worker. See the
[operating guide](Phase2A_Execution/README.md) before using the live adapter.

The entry trigger is connected later through:

```python
engine.request_entry(request_id="unique-stable-id", stock="NSE:YOUR_SYMBOL", budget="1000")
```

No prediction/training pipeline is required for this execution-only workflow.
The existing Phase 2 models, cost/P&L library and Phase 3 portfolio components
remain available as separate research/integration components. Their previous
status is recorded in [the extraction README](docs/EXTRACTION_README.md).
