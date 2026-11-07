# Reliable market data and measured exit response

Priority 5 uses **measured REST polling**. Live target/stop GTTs remain broker
owned. A quote observation never authorizes a competing sell while an unresolved
order or GTT owns the exit. The entry signal remains external.

## Timing configuration

| Field | Default | Meaning |
|---|---:|---|
| `poll_interval` | 5 s | Intended time between worker cycle starts; minimum 3 seconds. |
| `poll_cycle_budget` | 20 s | Maximum healthy cycle duration, including session checks/preflight. Overruns alert and pause entries. |
| `broker_request_timeout` | 5 s | SDK HTTP timeout per request, not a total-cycle deadline. |
| `reconnect_max_delay` | 30 s | Maximum backoff for a flat account. Exposure keeps the shorter polling interval after failed cycles. |
| `exit_response_timeout` | 60 s | Submission deadline per required sell or observed broker price trigger. |
| `max_quote_age` | 15 s | Freshness limit for exchange time, last-trade time and cycle-local reuse. |

The scheduler subtracts work time from its wait. Overruns skip missed slots
instead of creating catch-up work. `run --interval N` overrides `poll_interval`
through the same validation. Cycle start/completion, duration, maximum duration,
actual/maximum start gap, last success, failures and retry times are persisted.
Durations use a monotonic clock; disagreement with wall time raises an alert.
An interrupted cycle leaves a durable recovery marker.

Slow cycles defer additional trades at trade boundaries; the next cycle begins
with the first deferred trade. At least one trade is processed so a slow daily
preflight cannot permanently starve exits. A single SDK call is not forcibly
interrupted. The cycle is healthy only when all required work/data checks finish
within budget. Increasing positions beyond the default five requires measuring
the resulting workload. Startup and instrument downloads may exceed the budget;
that condition is reported rather than silently treated as healthy.

## Data validation and reconciliation

Each live cycle batches quotes for active stocks in open execution windows, up
to 500 per request. Quote calls remain at least 1.05 seconds apart, including
after errors; other calls retain adapter spacing. Idle cycles do not download
the entire approved universe. An entry fetches its own stock's quote.

Both exchange and last-trade timestamps must be present, recent and no more than
two seconds ahead. Prices must be positive and finite. Missing symbols, invalid
values, stale/future timestamps and timestamp regressions invalidate that symbol.
Validated watermarks survive restarts/reconnections. Duplicate timestamps are
allowed while fresh, but receiving a duplicate cannot refresh its trade time.

Only validated current-cycle prices can be reused, and freshness is rechecked at
use time. Failed/partial requests never reuse last-good prices. One bad symbol
does not stop order/GTT reconciliation for other symbols. Health reports missing
fresh data for active stocks during execution hours; normal closure is not a
feed-failure alert. Illiquid stocks with old last trades are deliberately rejected
even when their quote packets are recent.

Known order updates share a fresh daily order-book snapshot where possible.
Missing orders require authoritative history; missing history never proves
cancellation/absence. Snapshots expire during slow cycles and are invalidated
after submit/cancel attempts, including lost acknowledgements. Duplicate IDs,
wrong identity, backward fills, future/backward update times or disappearance of
a previously supplied update timestamp block reconciliation. Exact GTT child IDs,
confirmed terminal status and final fills remain mandatory before replacement.

## Connection recovery

A failed cycle discards its local snapshots and the HTTP transport. The next
attempt verifies the replacement connection/session; it never replays trading
POSTs. Flat accounts use capped backoff; exposure keeps the shorter reconciliation
interval. A renewed token can bypass a pending wait but still requires account
verification. Token revocation rules remain in force.

One successful API call cannot clear recovery. Fresh data, account/instrument and
inventory checks, order/GTT reconciliation, and a complete cycle within budget
are required. Unknown intents retain their original tags, IDs and reservations.
Outside pre-market/trading hours, a required full preflight waits for its eligible
window; existing positions still reconcile. The entry pause remains until an
operator runs `resume` after health clears. Existing exits continue through their
normal ownership checks. See [the session guide](SESSIONS_AND_MARKETS.md).

## Submission deadlines and their limits

Each required engine sell records an `exit_response` episode: observed requirement
time, source quote time when available, eligible start/deadline, SDK dispatch time,
and acknowledgement time. The live `sent_at` callback runs **after** rate spacing
and session verification, immediately before calling the SDK. Paper/custom
adapters without that callback measure adapter invocation instead. An order ID
acknowledgement is not a fill.

The maximum healthy engine response is **60 seconds from an observed, eligible
exit requirement to SDK submission**, by default. Each residual sell gets a new
episode. A request initially made outside hours waits to start its timer. Once
started, its deadline survives restarts, config changes, disconnections and market
closure. `EXIT_RESPONSE_LATE` exposes a breach; it never bypasses pending orders,
unconfirmed cancellations, GTT ownership, authorization or inventory checks. Safe
late recovery remains permitted.

For a persistent price condition on a healthy polling path, the planning bound is
`2 * poll_cycle_budget + poll_interval`: **45 seconds** with defaults. This allows
one cycle phase, one interval and the following cycle. It assumes continuing
fresh quotes, all positions fitting within budget, ownership already clear, no
cancellation/retry, and an available session/market. The response timeout cannot
be configured below this bound. Polling can miss a transient crossing between
samples; live GTTs watch the broker's feed independently.

A fresh observed price beyond a saved level while its GTT still reports active
starts a separate 60-second watch. `BROKER_TRIGGER_DELAY` reports missing timely
child submission evidence without creating a competing sell. Every discovered
child records the broker order timestamp and local discovery time. When no price
crossing was locally observed, its actual trigger time remains **unknown**, not
an invented zero latency. Missing broker order timestamps cannot certify timely
submission either.

These are monitored operating limits, **not guarantees of submission or fills**.
Outages, blocked requests, rate limits, closure or ownership uncertainty can
exceed them. SDK timeouts do not impose a hard deadline on all work. Broker GTT
trigger-to-submission timing cannot be certified solely from local polling.

## Verification and monitoring

```bash
python -m unittest discover -s tests -p test_market_data.py -v
python -m execution --config config.live.json --db state/live.sqlite --live run
python -m execution --config config.live.json --db state/live.sqlite health
```

The deterministic five-position test injects a persistent crossing, a 25-second
detection phase and delayed ownership checks. Final dispatch occurs at 35 seconds,
within the conditional 45-second bound and 60-second deadline. This is a synthetic
fault/load test, not a live benchmark. Tests also cover bad/frozen/regressive data,
partial batches, reconnect/backoff, recovery gates, lost acknowledgements, fair
processing after overruns, stale order snapshots, clock changes and deadline
breaches without competing sells. SDK contract tests use no real credentials or
network orders.

Read-only `health`, `status` and `journal` can inspect committed SQLite state while
the execution lock is held, so an independent monitor can detect a stuck cycle.
They are observations of committed state, not an atomic multi-table snapshot
while the worker is updating it. `POLL_STALE`, `POLL_CYCLE_OVERRUN`,
`POLL_CLOCK_CHANGED`, `POLL_RECOVERY_REQUIRED`, `MARKET_DATA_UNAVAILABLE` and
per-trade response alerts use existing structured stdout/stderr output. A fresh
heartbeat during backoff does not claim fresh reconciliation. External paging is
not configured; connect your supervisor/log collector.

Actual-account timing acceptance remains pending. Measure cycle durations and
broker child timestamps in the deployed account, exercise data loss/recovery and
verify alert delivery alongside [account acceptance](ACCOUNT_ACCEPTANCE.md).
Synthetic tests or `HEALTHY` do not establish live performance or guaranteed fills.

Primary contracts reviewed on 7 November 2026:
[quotes and timestamps](https://kite.trade/docs/connect/v3/market-quotes/),
[rate limits/errors](https://kite.trade/docs/connect/v3/exceptions/), and
[daily order books/history](https://kite.trade/docs/connect/v3/orders/).
