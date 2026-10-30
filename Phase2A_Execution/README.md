# TradeX execution engine

This module owns everything from an explicit **entry request** to a confirmed
exit. The buy signal is an external input, left for later. Scope is one account,
fully funded long-only CNC equity trades on NSE/BSE, whole shares, and a fixed
approved universe. It runs independently of the prediction and portfolio models.

## Quick start

From this directory, with Python 3.11+ on Linux/macOS:

```bash
python demo.py
python -m unittest discover -s tests -v
```

For persistent paper operation, each command uses the same configuration and
`state/engine.sqlite` / `state/paper.sqlite` databases:

```bash
python -m execution quote NSE:DEMO 100
python -m execution entry experiment-1 NSE:DEMO 1000
python -m execution tick
python -m execution status
# Use the calculated target/stop from status. 110 crosses the demo target:
python -m execution quote NSE:DEMO 110
python -m execution tick
python -m execution tick
python -m execution journal
```

`entry` is an explicit operator/test request, not the future entry strategy.
Request IDs are permanent idempotency keys. Repeating an ID with identical inputs
returns the existing trade; reusing it for different inputs fails.

`python -m execution run` polls continuously (default 5 seconds between cycles).
Paper quotes must be supplied by a replay/feed or `quote`; the engine never
fabricates or silently refreshes market prices. Quotes older than the configured
age are rejected. Ctrl+C stops the worker, and a subsequent run recovers state.

## One-time configuration

`config.paper.json` uses fictional stocks and illustrative percentages/fee
assumptions. It is not a recommended trading strategy or a current broker fee
schedule. Copy it to ignored `config.live.json` when configuring a real account.

| Field | Meaning |
|---|---|
| `stocks` | Exact `EXCHANGE:SYMBOL` -> tick size whitelist. Kite validates against its instrument master. |
| `capital_limit` | Maximum sum of reserved trade budgets; no leverage. |
| `per_trade_limit` | Maximum entry budget, including estimated buy fees. |
| `profit_pct` | Desired net profit percentage on filled entry notional, after estimated fees. |
| `stop_pct` | Gross price distance below average fill; this is not a guaranteed maximum net loss. |
| `fees` | Per-side basis points and fixed charges per order. Include a delivery/DP allowance where appropriate. |
| `max_positions` | Maximum concurrent entries/positions. |
| `max_quote_age` | Freshness limit in seconds (also rejects future timestamps). |
| `entry_timeout` | Cancel a nonterminal entry after this many seconds. Partial entries are cancelled immediately. |
| `max_exit_attempts` | Bounded retries after confirmed terminal partial/unfilled exits. |

Money uses `Decimal`. Settings for tick size, fees, target and stop are snapshotted
per trade; restarting with a new target percentage does not move an open trade's
exit levels. Removing a symbol from the whitelist prevents new entries but does
not stop managing its existing position.

The entry limit is rounded up to a tick at 0.2% above the fresh quote, and quantity
is rounded down so the worst-case entry notional plus estimated buy charges fits
the budget. IOC cancels the unfilled remainder. Exits are sized only after the
entry is confirmed terminal, including cancellation confirmation after a partial
fill. There is no averaging down, shorting or leverage.

## Automatic exit calculation

For average fill `P`, filled quantity `Q`, net target fraction `r`, estimated buy
fees `B`, sell fixed fee `F` and sell proportional rate `s`:

```text
target = round_up_to_tick((P*Q + B + P*Q*r + F) / (Q*(1-s)))
stop   = round_up_to_tick(P*(1-stop_pct/100))
```

The stop must remain strictly below the average fill. If tick size makes that
impossible after a fill, the engine requests an exit instead of inventing an
invalid stop. Example demo: 9 shares filled at 100, 2% net target, 1% stop, 15 bps
per side and a 20 fixed sell allowance -> target 104.55, stop 99.00.

Targets assume one exit order. Additional partial-exit orders, actual broker
charges and slippage can change realized net profit. The journal labels P&L
`estimated_net_pnl`; it is not reconciled contract-note P&L. The existing Phase 2B
cost engine is preserved separately; this module uses the explicit fee profile
above rather than claiming that its rates have been validated for your account.

## State and recovery

`ENTERING -> OPEN -> EXITING -> CLOSED`, or `ENTERING -> NO_FILL`.
Each broker intent is committed before submission. A lost submission response
leaves an UNKNOWN intent. Recovery searches the broker tag; it does **not** retry
the POST merely because no response was received. Tags are not assumed to be
broker-enforced idempotency keys. Terminal fills are journaled locally because
the Kite order book is daily.

One SQLite database is bound to one broker account/mode. Use exactly one database
and one worker deployment per account, on local durable disk, and keep manual
trading/other bots away from managed symbols. A file lock prevents overlapping
operations using that same database. Multiple databases/hosts are not coordinated.
Back up the database using SQLite's backup API or after stopping the worker; a
live WAL database must not be backed up by copying only the main file.

The worker checks holdings/positions and foreign pending orders before entry or
exit. Mismatches, stale data and broker errors are journaled and pause new entries.
Other positions continue to be processed. The heartbeat and per-trade errors are
available in `status`; JSON logs go to stdout/stderr for your process supervisor.

```bash
python -m execution pause             # blocks buys; exits continue
python -m execution resume            # requires active errors resolved
python -m execution flatten           # pause + request all exits; keep worker running
python -m execution flatten --request-id experiment-1
python -m execution retry-exit experiment-1
```

`retry-exit` allows one extra attempt only after earlier exits have confirmed
terminal states. First inspect and fix the broker rejection (authorization,
funds, market restrictions, etc.). An UNKNOWN intent keeps its capital reservation
until reconciled. If broker records **manually confirm it was never placed**:

```bash
python -m execution resolve-absent experiment-1 INTENT_TAG --confirmed-not-placed
python -m execution tick
```

Never attest absence from an empty daily order book alone. If the order exists,
let normal tag reconciliation adopt it. Cross-day unresolved submissions may
require broker support; do not delete the state or issue a replacement blindly.

## Optional Kite connection

```bash
python -m pip install -r requirements-live.txt
# Set KITE_API_KEY and KITE_ACCESS_TOKEN securely in the worker environment.
python -m execution --config config.live.json --db state/live.sqlite --live run
```

The SDK version is pinned and contract-tested. The adapter validates stock/tick
metadata at startup, uses account identity binding, sends IOC LIMIT buys and IOC
MARKET sells with Kite automatic market protection, and applies request spacing.
An API order ID is only an acknowledgment: fills come from order history. No
live broker calls or trades were used to validate this implementation. Do a
controlled account integration check before relying on it unattended.

CNC holdings may require depository sell authorization. Supply the correct
account setup; this engine does not bypass that requirement. Session access
tokens must be obtained/refreshed outside this engine; credentials are read only
from environment variables and never written into the trade journal.

Stops and targets are **software triggers**, not broker-hosted OCO/GTT orders.
Only one sell can be outstanding, so target and stop cannot independently sell
the same position. The worker must be online with a fresh feed. Shutdowns, API
outages, gaps, circuits and protected market orders that do not fill can leave
positions open. An entry awaiting cancellation confirmation is not yet eligible
for an exit. Run with a supervisor and alert on stale heartbeat or trade errors;
broker-hosted protection and live operational acceptance remain deployment work.

## Entry strategy boundary

```python
engine.request_entry("stable-event-id", "NSE:APPROVED_SYMBOL", "1000")
engine.tick()  # the worker repeats this to reconcile orders and manage exits
```

The caller chooses only the approved stock, budget and event ID. It does not need
to specify entry, target or stop prices. Deciding **when** to call this interface
is the intentionally deferred part of the product.

Official adapter references: [orders](https://kite.trade/docs/connect/v3/orders/),
[quotes](https://kite.trade/docs/connect/v3/market-quotes/),
[portfolio](https://kite.trade/docs/connect/v3/portfolio/).
