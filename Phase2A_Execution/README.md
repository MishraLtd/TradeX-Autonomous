# TradeX execution engine

For the current ₹4,000 intraday experiment with real Upstox quotes, fixed
long/short entry times and entirely local simulated orders, use
[`execution.paper_trial` and its Azure runbook](../deploy/UPSTOX_PAPER.md).
That mode has an independent account and no live-order path. The CNC workflow
documented below remains separate; do not use `--live` for the Upstox trial.

This module owns everything from an explicit **entry request** to a confirmed
exit. An operator supplies the buy decision through the controlled CLI/API.
See [controlled entry](CONTROLLED_ENTRY.md) for authentication and remote access.
Scope is one account,
fully funded long-only CNC equity trades on NSE/BSE, whole shares, and a fixed
approved universe. It runs independently of the prediction and portfolio models.

## Quick start

From this directory, with Python 3.11+ on Linux/macOS:

```bash
python demo.py
python demo_recovery.py
python -m unittest discover -s tests -v
```

For persistent paper operation, each command uses the same configuration and
`state/engine.sqlite` / `state/paper.sqlite` databases. First start
`python -m execution run` in a separate terminal and leave it running; new CLI/API
entries require its fresh heartbeat. Then use:

```bash
python -m execution quote NSE:DEMO 100
python -m execution entry experiment-1 NSE:DEMO 1000
python -m execution tick
python -m execution status
# Use the calculated target/stop from status. 110 crosses the demo target:
python -m execution quote NSE:DEMO 110
# The paper broker has already triggered and filled without an engine tick.
python -m execution tick  # reconcile the broker's sell and close the trade
python -m execution journal
```

`entry` is an explicit operator request and returns `{replayed, trade}`.
Request IDs are permanent idempotency keys. Repeating an ID with identical inputs
returns the existing trade; reusing it for different inputs fails.

`python -m execution run` polls continuously (default 5 seconds between cycle
starts, subtracting work time and skipping missed slots).
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
| `exit_mode` | `BROKER_OCO` (default) registers paired broker GTT exits. Live Kite requires this mode. `SOFTWARE` is available only for paper comparisons. |
| `stop_limit_buffer_pct` | Stop sell limit distance below the stop trigger, rounded down to a tick (default 0.5%). A larger buffer accepts a lower execution price; it does not guarantee a fill. |
| `fees` | Legacy/paper per-side basis points and fixed charges per order; remains the paper broker's simulated cash-fee model. |
| `cost_profile` | Reviewed account-bound component cost profile JSON path, required for new live entries. See [costs and reconciliation](COSTS_AND_RECONCILIATION.md). |
| `max_positions` | Maximum concurrent entries/positions. |
| `max_quote_age` | Freshness limit in seconds (also rejects future timestamps). |
| `entry_timeout` | Cancel a nonterminal entry after this many seconds. Partial entries are cancelled immediately. |
| `max_exit_attempts` | Total sell attempts per trade, including a GTT child (default 3). A replacement requires the previous sell to be terminal. |
| `exit_timeout` | Maximum seconds before cancelling a pending sell, including a partially filled GTT child (default 30). |
| `cancel_timeout` | Seconds from the first cancellation request to critical escalation (default 15). |
| `max_cancel_attempts` | Maximum cancellation requests for each order/GTT (default 3); exhausting this also escalates. |
| `submission_timeout` | Seconds before an uncertain submission or continuing reconciliation failure escalates to critical (default 30). No blind POST retry. |
| `retry_interval` | Minimum seconds between cancellation requests and cooldown after confirming a terminal sell before replacing it (default 5). |
| `emergency_exit_policy` | `BROKER_PROTECTED_MARKET` permits confirmed exits through a capable broker when local quotes fail; `WAIT_FOR_FRESH_QUOTE` requires a fresh quote. |
| `health_max_age` | Maximum heartbeat/reconciliation age for the read-only health check (default 30 seconds). |
| `live_account_id` | Exact expected Kite user ID, required before new live entries. Leave null in paper mode. |
| `market_calendar` | Optional reviewed calendar JSON path for live execution. Default is the bounded bundled NSE/BSE schedule; unknown dates block order actions. |
| `poll_interval` | Intended seconds between polling starts (default 5, minimum 3). |
| `poll_cycle_budget` | Maximum healthy cycle duration (default 20 seconds). Overruns alert, pause entries and rotate deferred work. |
| `broker_request_timeout` | Live SDK HTTP timeout per request (default 5 seconds); not a total-cycle deadline. |
| `reconnect_max_delay` | Maximum backoff without exposure (default 30 seconds); active exposure keeps the polling interval. |
| `exit_response_timeout` | Submission-response deadline per sell or observed broker trigger (default 60 seconds). Must cover two cycle budgets plus one interval. |

Money uses `Decimal`. Tick size, fees, target, stop, exit mode and stop-limit
buffer are snapshotted per trade; restarting with new percentages does not move
an open trade's exit levels. An existing GTT remains authoritative even if the
configuration changes. Removing a symbol from the whitelist prevents new entries
but does not stop managing its existing position.

Recovery settings (deadlines, attempt limits, retry interval and emergency policy)
are also snapshotted per trade. Existing journals acquire them on their next
reconciliation. Restarting with larger limits does not reset an existing deadline
or cancellation budget. Monitoring freshness uses the current configuration.

The entry limit is rounded up to a tick at 0.2% above the fresh quote, and quantity
is rounded down so the worst-case entry notional plus estimated buy charges fits
the budget. IOC cancels the unfilled remainder. Exits are sized only after the
entry is confirmed terminal, including cancellation confirmation after a partial
fill. There is no averaging down, shorting or leverage.

## Automatic exit calculation

For legacy/paper `fees`, average fill `P`, filled quantity `Q`, net target fraction `r`, estimated buy
fees `B`, sell fixed fee `F` and sell proportional rate `s`:

```text
target = round_up_to_tick((P*Q + B + P*Q*r + F) / (Q*(1-s)))
stop   = round_up_to_tick(P*(1-stop_pct/100))
```

The stop must remain strictly below the average fill. If tick size makes that
impossible after a fill, the engine requests an exit instead of inventing an
invalid stop. Example demo: 9 shares filled at 100, 2% net target, 1% stop, 15 bps
per side and a 20 fixed sell allowance -> target 104.55, stop 99.00.

That legacy formula assumes one exit order. New live entries require an account
cost profile: targets reserve conservative delivery/intraday component charges
and up to `max_exit_attempts` filled sells, including DP and rounding allowances.
Whole-share sizing includes the buy allowance. Partial same-day CNC exits can
incur both full-order brokerage and delivery taxes, so the model covers that
mixture. The profile is saved with the trade; existing targets remain unchanged.

Closure P&L remains `estimated_net_pnl`. The new `reconcile-finances` command
compares independent broker evidence with fills, owned shares, cash and realized
P&L, then records matched actual/allocated costs in `financials`. It preserves
original estimates and blocks new live entries on missing prior-day evidence or
financial discrepancies, while existing exits continue. See the
[costs and reconciliation guide](COSTS_AND_RECONCILIATION.md) for profile setup,
evidence schema, source checks, daily gates and current limitations. Real-account
financial acceptance remains pending; the Phase 2B research library stays separate.

## Broker-held target and stop

After the entry reaches a confirmed terminal state, the engine calculates levels
from the actual fills and registers a two-leg sell GTT. The stop leg submits a
limit sell at `floor_to_tick(stop * (1 - stop_limit_buffer_pct / 100))`, with a
minimum of one tick; the target leg submits a limit sell at the target. Both use
the filled quantity and CNC product. A fresh quote already outside the target or
stop causes an immediate exit request instead of registering invalid levels.

The engine journals its intent before registration, saves the broker trigger ID,
then reads the GTT back to verify the stock, quantity, side, product and prices.
Only a verified `ACTIVE` trigger is reported as active protection. New entries
are blocked while an existing position has missing, stale or uncertain protection.
Existing live journals gain protection during reconciliation without replacing
their saved target or stop prices.

Once registered, the broker watches the prices even if the worker or its network
connection fails. The engine discovers a triggered sell through the GTT's child
order ID, confirms its actual fills and sells only any confirmed residual after
that order is terminal. It never treats a GTT trigger as a completed sale. A
worker pause leaves existing protection in place.

`flatten` first requests GTT cancellation, then fetches its state again. An
acknowledgment alone cannot authorize another sell. If a trigger fires during
cancellation, the engine adopts its child order; a pending child must be cancelled
and its final fill quantity confirmed before a replacement is submitted. Expired,
deleted or otherwise lost protection pauses new entries and requests closure once
the broker confirms no competing trigger/child remains. Unknown or inconsistent
responses block replacement sells and appear in the journal/status.

### Limits of this protection

- The interval between an entry fill and verified GTT registration remains
  unprotected, including while a partially filled entry awaits cancellation.
- GTTs submit limit orders. A gap below the stop limit can leave the sell pending;
  no maximum loss or execution price is guaranteed. One leg firing consumes the
  OCO trigger even if its order remains unfilled. A pending target order therefore
  does not retain the sibling stop. The worker now applies a sell deadline and
  cancels/replaces only after confirming terminal status and final fills. This
  recovery cannot run while the worker is offline or the broker is unreachable.
- GTTs can expire, be disabled, or fail to submit an order. The worker must run to
  detect these conditions and reconcile/follow up; it cannot repair them offline.
- Broker sell authorization, trading restrictions and market liquidity still
  apply. Session renewal now has a supported workflow; actual live-account
  acceptance evidence is still pending.
- The paper broker models GTTs independently on each injected fresh quote. It
  models gaps and partial fills, but not market calendars, order-book liquidity,
  or automatic exchange end-of-day cancellation; it is not a live certification.

## Stalled-order recovery

The worker evaluates deadlines each cycle. A deadline is a reason to escalate,
never evidence that an order failed or that shares can safely be sold again.

| Condition | Automatic response | When intervention is required |
|---|---|---|
| Sell remains pending or partially filled past `exit_timeout` | Pause new entries, request cancellation, confirm terminal state/fills, then retry only the residual after the cooldown. | Cancellation cannot be confirmed, or the sell attempt limit is reached. |
| Entry/order/GTT cancellation is delayed | Persist the first deadline and attempt count; space repeated cancellation requests. Continue polling for authoritative state. | Deadline or cancellation budget expires: `CANCEL_STALLED` becomes critical and further automatic cancellation requests stop. |
| Exit is rejected or terminates unfilled | Record the rejection message when supplied, wait `retry_interval`, and retry within `max_exit_attempts`. | `EXIT_ATTEMPTS_EXHAUSTED`: fix the broker issue and use `retry-exit` to allow one extra attempt. |
| Regular order or GTT submission is uncertain | Preserve the original intent and capital reservation; reconcile without repeating the submission. | Uncertainty passes `submission_timeout`: critical alert, with the existing explicit adoption/absence-verification procedures. |
| Broker/state reconciliation keeps failing | Keep processing other trades and record the error and last successful reconciliation. | Failure passes `submission_timeout`, or the successful reconciliation becomes stale. |

An exit timeout uses the original order deadline, not time since the latest
partial fill. For GTT children discovered after downtime, broker placement time
is used when available; otherwise the first local observation is the fallback.
Confirmed late cancellation or execution can still resolve an escalated trade
automatically. If cancellation remains uncertain, inspect/cancel it at the broker
and let the worker verify the result; `retry-exit` cannot bypass a pending order.

Timeout replacement sells use the existing protected market exit path. They may
execute below the original target or stop and incur additional charges; they
prioritize closing the confirmed remaining exposure. Exchange circuits, market
closure, liquidity and broker authorization can still prevent execution. Keep the
worker running and route critical alerts to an operator; automatic retries are
deliberately bounded.

### Emergency exits when quotes fail

The default `BROKER_PROTECTED_MARKET` policy applies only when a decision to exit
already exists, such as `flatten`, a broker-triggered exit, or lost protection.
The engine first reconciles order fills, verifies GTT release and checks inventory
and foreign orders/triggers. If the local quote is missing, stale or unavailable,
the Kite adapter can then send an IOC MARKET sell with broker market protection.
No old quote is used to invent a limit price. The event and feed failure are
journaled and reported in health; broker submission/fill uncertainty still uses
the same recovery checks.

`WAIT_FOR_FRESH_QUOTE` opts out of that behavior and exposes the blocked exit as
an alert. Entries and price-based decisions to start an exit always require a
fresh quote. The paper broker does not support quote-free execution and always
requires a fresh injected price for a fill. Neither policy bypasses unknown
orders, unconfirmed cancellations, ownership mismatches, or broker restrictions.

### Execution health and alerting

```bash
python -m execution status
python -m execution health
# For a deployed account, use its configuration and journal:
python -m execution --config config.live.json --db state/live.sqlite health
```

`health` performs no broker calls and needs no credentials. It returns JSON plus
exit code **0** for `HEALTHY`, **2** for `DEGRADED`/`CRITICAL`; command/configuration
failures return **1**. It checks worker freshness, each trade's successful
reconciliation, protection verification, unresolved submissions, delayed cancels,
unprotected exposure and exhausted exits. A fresh heartbeat cannot hide failing
trades. A stopped worker or stale protection is detected using current time even
if no more engine events are written.

`run` includes health in each stdout record and writes `execution_alert` JSON to
stderr when an alert first appears or changes severity. Alerts include a stable
code, trade/order reference, deadline where applicable, and recovery action;
alert changes are also journaled. The current health snapshot is authoritative
for resolved alerts. `tick` emits the current alerts as well. No email, messaging,
or paging service is configured by this code: connect your process supervisor/log
collector and poll `health` independently to detect worker outages.

Alerts pause new entries while reconciliation continues. After recovery, the
pause remains in place until `resume`; that command refuses active errors or
execution alerts. `HEALTHY` with `paused: true` means reconciliation is healthy
but new buys remain deliberately disabled. Choose monitoring thresholds and a
poll interval that accommodate the expected number of positions and API latency.

Live polling batches and validates quotes, reconciles order updates from fresh
snapshots, and requires a complete verified cycle after disconnection. The
[market-data and latency guide](MARKET_DATA_AND_LATENCY.md) defines response
deadlines, conditional timing bounds and the need for actual broker measurements.
Read-only status/health/journal can inspect committed SQLite state while the
execution lock is held, allowing an independent monitor to detect a stuck cycle.

Session, calendar and preflight alerts also appear in health. These readiness
gates block new entries without clearing an existing manual pause. Read the
[sessions and market-hours guide](SESSIONS_AND_MARKETS.md) for daily login,
calendar maintenance and the difference between a running worker and a verified
broker session. Normal execution-window closure does not cause feed alerts.

## State and recovery

`ENTERING -> OPEN -> EXITING -> CLOSED`, or `ENTERING -> NO_FILL`.
Each broker intent is committed before submission. A lost regular-order response
leaves an UNKNOWN intent. Recovery searches the broker tag; it does **not** retry
the POST merely because no response was received. Tags are not assumed to be
broker-enforced idempotency keys. GTT creation has no client tag/idempotency key;
its separate recovery procedure is below. Terminal fills are journaled locally
because the Kite order book is daily.

One SQLite database is bound to one broker account/mode. Use exactly one database
and one worker deployment per account, on local durable disk, and keep manual
trading/other bots away from managed symbols. A file lock prevents overlapping
operations using that same database. Multiple databases/hosts are not coordinated.
Back up the database using SQLite's backup API or after stopping the worker; a
live WAL database must not be backed up by copying only the main file.

The worker checks holdings/positions, foreign pending orders and unmanaged GTTs
before entry or a replacement exit. Mismatches, stale data and broker errors are
journaled and pause new entries. Other positions continue to be processed. The
heartbeat, per-trade errors and `protection` record are available in `status`;
that record includes the trigger ID, expected contract, health, verification time
and child IDs. JSON logs go to stdout/stderr for your process supervisor.

```bash
python -m execution pause             # blocks buys; exits continue
python -m execution resume            # requires active errors and alerts resolved
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

### Lost GTT registration response

If registration times out before its ID is saved, the engine pauses new entries
and neither repeats the POST nor sends a competing sell. Inspect the broker GTT
and order records to identify the exact trigger created for the journaled intent:

```bash
python -m execution adopt-protection experiment-1 EXACT_BROKER_TRIGGER_ID
python -m execution tick
```

The command verifies the saved prices and quantity, rejects an older or already
owned trigger, and blocks adoption if another unmanaged trigger remains. It also
works when that GTT has already triggered, provided the child order can still be
verified. Matching prices alone are not evidence of ownership; the operator must
identify the exact broker record. If no trigger was created, **first verify that
fact in broker records**, then explicitly attest it:

```bash
python -m execution resolve-protection-absent experiment-1 --confirmed-not-created
python -m execution tick
```

This enables closure of the unprotected position, not another registration
attempt. An empty GTT list alone is insufficient: the list of non-active GTTs is
time-limited, and an unknown trigger may have fired. Missing trigger/history
records, unresolved child submissions or identity changes require manual broker
reconciliation. Never delete the journal to clear these errors. A saved trigger
ID is fetched directly during normal restart recovery; no operator adoption is
needed simply because the worker restarted. Resume new entries only after errors
are resolved and existing exposure has been reconciled.

## Optional Kite connection

Follow the [account acceptance guide](ACCOUNT_ACCEPTANCE.md) before relying on a
live account. The read-only `validate-account` command checks actual account
responses and records missing execution/settlement evidence. Synthetic tests do
not establish live acceptance; real-account validation remains pending until
those observations are collected and reviewed.

```bash
python -m pip install -r requirements-live.txt
# Set KITE_API_KEY securely in the worker environment.
# Set live_account_id and approved real instruments in config.live.json.
python -m execution --config config.live.json login-url
# Complete Kite login, then enter the returned request token at the hidden prompt:
python -m execution --config config.live.json login
python -m execution --config config.live.json --live validate-account
python -m execution --config config.live.json --db state/live.sqlite --live preflight
python -m execution --config config.live.json --db state/live.sqlite --live run
```

The SDK version is pinned and contract-tested. The adapter validates stock/tick
metadata at startup, uses account identity binding, sends IOC LIMIT buys, paired
CNC LIMIT GTT exits, and IOC MARKET sells with Kite automatic market protection
for confirmed fallback/operator exits. It applies request spacing. An API order
ID is only an acknowledgment: fills come from order history. No
live broker calls or trades were used to validate this implementation. Do a
controlled account integration check before relying on it unattended.

CNC holdings may require depository sell authorization. Supply the correct
account setup; this engine does not bypass that requirement. Complete the
supported manual Kite login before each trading day; the engine exchanges the
request token, stores a private receipt and reloads replacements automatically.
Tokens are never written into the trade journal. See
[SESSIONS_AND_MARKETS.md](SESSIONS_AND_MARKETS.md) for expiry/revocation handling,
the reviewed calendar (currently through December 2026), pre-market validation
and next-day recovery. Paper mode remains independent of wall-clock market hours.

Keep `exit_mode` set to `BROKER_OCO`; live startup rejects `SOFTWARE`. Run with a
supervisor and monitor the `health` command and structured execution alerts.
Verified GTTs survive worker downtime, but startup/readback and triggered-order
recovery still depend on broker API availability and retained order records.
Live operational acceptance remains deployment work.

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
[portfolio](https://kite.trade/docs/connect/v3/portfolio/),
[GTT](https://kite.trade/docs/connect/v3/gtt/).
