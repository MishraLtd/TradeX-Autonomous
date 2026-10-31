# Sessions, market hours and next-day recovery

Priority 4 adds a supported Kite login workflow, private session storage and
renewal detection, expiry/revocation alerts, a bounded exchange calendar,
pre-market validation, and recovery across trading days. The entry signal is
still supplied separately. Tests use synthetic accounts and orders; real-account
operational acceptance remains pending in [ACCOUNT_ACCEPTANCE.md](ACCOUNT_ACCEPTANCE.md).

## Login and daily renewal

Run these commands from `Phase2A_Execution` in a local interactive terminal.
Install `requirements-live.txt`, configure `live_account_id`, approved real
symbols/tick sizes and limits in ignored `config.live.json`, and set
`KITE_API_KEY` securely in the environment. The API secret can be supplied through
`KITE_API_SECRET` or entered at the hidden prompt. Do not paste credentials into
chat, command arguments, source files or logs.

```bash
python -m execution --config config.live.json login-url
# Open the URL yourself, sign in to Kite and complete its authentication.
# The registered redirect URL receives a short-lived request_token.
python -m execution --config config.live.json login
```

`login` prompts privately for the request token, exchanges it once using the
official SDK, checks the returned account and API key, and atomically writes
`state/live.kite-session.json` with owner-only permissions. It prints account and
expiry metadata, never the access token, API secret or request token. A failed
exchange is not retried automatically; obtain a new request token. The worker
requires the private file to be a regular file owned by its OS user with no
group/other permissions; symlink receipts are rejected. Use a dedicated account
and private state directory on durable local disk.

Kite documents normal expiry at **06:00 IST on the day after login**; a master
logout can invalidate the token earlier. This implementation does not automate
passwords/OTP, use browser cookies, or assume trading tokens can be refreshed
without login. Repeat the supported login flow after expiry/revocation, normally
before each trading day. The same file is replaced; a running worker reloads it
before API calls, verifies the account, and repeats preflight on token change.
An invalid/missing/revoked receipt blocks broker calls. A revoked token remains
blocked across process restarts until a different valid token is supplied.

The default receipt is ignored by Git. For another location, pass the **same**
global `--session-file state/other.kite-session.json` option to login and worker
commands, before the subcommand. Keep it inside ignored `state/`. A migration
path accepts `KITE_ACCESS_TOKEN` only with an explicit
`KITE_ACCESS_TOKEN_ISSUED_AT` (ISO timestamp, preferably including `+05:30`) when
the file is absent. A malformed or expired file never falls back to environment
credentials. Prefer the renewable file; changing a parent shell's environment
cannot update an already-running worker.

## Worker and pre-market checks

```bash
python -m execution --config config.live.json --db state/live.sqlite --live run
python -m execution --config config.live.json --db state/live.sqlite --live preflight
python -m execution --config config.live.json --db state/live.sqlite health
```

`run` stays alive with a fresh worker heartbeat while waiting for a missing,
expired or revoked session file. Health remains critical and broker verification
timestamps are not refreshed. Once the replacement passes account verification,
the same worker resumes reconciliation. An unavailable broker or failed startup
is retried through reads; trading POSTs are never automatically replayed.
An absent API key, wrong account configuration, missing SDK or malformed calendar
is a configuration problem to fix before starting the worker.

Preflight runs during the **45 minutes before the supported opening** and when
starting during trading. It repeats on a new IST trading date, token replacement,
or explicit `preflight`. It checks account permissions, refreshed instrument
metadata, usable equity cash, known order/GTT state, account inventory, unmanaged
orders and unmanaged triggers. Portfolio snapshots are shared across symbols to
avoid a full account download per approved stock. Preflight itself issues only
broker reads. Outside pre-market/trading it reports `WAITING`; it does not fetch
stale prices to decide readiness. A failed daily check blocks new entries while
the worker continues managing existing exits whenever safe. Existing error/pause
and execution-alert checks still apply.

## Exchange calendar and supported execution windows

The bundled [calendar](execution/calendar.json) has explicit coverage from
**31 October through 31 December 2026** for NSE/BSE cash equities. It is a
conservative execution schedule, not a complete timetable of exchange sessions:

| Day/session | Engine execution window, IST |
|---|---|
| Regular trading weekday | 09:15–15:15, excluding holidays |
| 8 November 2026, Sunday Muhurat | 14:20–15:15 |
| 10 November, 24 November, 25 December | Closed |
| Other Saturdays/Sundays | Closed unless explicitly listed as a special session |
| Date/exchange outside reviewed coverage | Unknown; actions blocked |

The 15:15 cutoff excludes the closing auction for CAS stocks and deliberately
also restricts non-CAS stocks to the same continuous-trading window. This remains
a conservative cutoff if an exchange later extends continuous trading. Pre-open,
closing auctions, post-close, illiquid call auctions and IPO/relisting special
pre-open trading are not supported entry sessions; restrict the approved universe
to ordinary continuous-trading equities. Exchange halts, circuits and security
restrictions still override any calendar and can prevent execution.

Entries need at least `max(60, entry_timeout + cancel_timeout + 30)` seconds left
in the window (75 seconds with defaults); other engine actions stop with 10
seconds left. These buffers reduce, but cannot eliminate, a boundary crossed
during network/exchange processing. There are no after-market-order submissions.
The engine reconciles reads after the window ends, including orders/GTT children
which execute during a later exchange session.

To maintain the calendar, copy `execution/calendar.json`, review current exchange
and broker circulars, update `valid_from`, `valid_through`, `reviewed_on`, holiday
dates, per-exchange `special` windows and source references, then set
`"market_calendar": "path/to/reviewed-calendar.json"` in the live config and
restart the worker. Paths are relative to the working directory. Coverage is
limited to at most one year. Use `unverified: ["YYYY-MM-DD"]` for a special date
whose schedule is not confirmed; that date is blocked and next-open calculations
do not jump over it. The file is deployment configuration, not an automatically
updated market-data feed. Review newly announced closures/session changes promptly.
Coverage expiry warns seven days ahead; uncovered dates fail closed. Updating the
year number alone is not a calendar update.

## Overnight behavior

The worker keeps reading known orders, GTTs and inventory while the execution
window is closed and authentication is valid. It does not request quotes,
cancel protection, send replacement sells, or spend cancellation/retry attempts
solely because the window is closed. `flatten` persists the exit request and
keeps the GTT in place until a supported session, then cancels, confirms release
and sizes the residual exit. It is a queued request, not confirmation of a sale.

Actual unresolved submissions, missing protection, stalled exits/cancellations
and failed broker reconciliation remain visible overnight. Deadlines and attempt
budgets are **not reset** on date changes or restarts. Normal closure is reported
as market state, without inventing a quote-feed failure. Broker-held triggers can
still operate according to broker rules even when the engine's window is closed.

The next day, confirmed terminal fills and target/stop levels are reused from the
journal; current holdings/positions and GTT state are checked before new entries.
Pending orders still require authoritative terminal status and final fills. Kite's
daily order book becoming empty does not prove cancellation or absence. If an
unresolved prior-day order or GTT child can no longer be retrieved, entries and
competing sells remain blocked for manual broker reconciliation. Never delete the
journal, clear an unknown intent from an empty book, or reuse its request ID.

## Monitoring and acceptance

`health` is read-only and does not require credentials. It recomputes token expiry
and calendar coverage even if the worker has stopped. Session warnings start one
hour before expiry; expiry, revocation, unavailable session checks, failed
preflight and unverified calendars are explicit alerts. `run` emits changed alerts
as JSON on stderr for a local supervisor/log collector. External paging delivery
is not configured. A fresh heartbeat is never proof of fresh broker reconciliation.
Account details are private operational data even though logs contain no tokens.

Before unattended operation, exercise daily login/renewal, deliberate revocation,
a closed-market cycle, the next opening, and next-day settled/T1 inventory with
the actual account. Review the read-only acceptance evidence and alert delivery.
Synthetic regression tests verify these code paths but do not establish actual
broker/session availability or guaranteed execution.

Primary references reviewed on 31 October 2026:

- [Kite supported login and token lifetime](https://kite.trade/docs/connect/v3/user/).
- [NSE equities holidays](https://www.nseindia.com/resources/exchange-communication-holidays?apiversion=v2).
- [NSE closing auction schedule](https://www.nseindia.com/static/products-services/closing-auction-session).
- [NSE Muhurat circular CMTR76941](https://nsearchives.nseindia.com/content/circulars/CMTR76941.pdf).
- [Zerodha's NSE/BSE operating holiday calendar](https://zerodha.com/marketintel/holiday-calendar/).
- [Zerodha Muhurat operating schedule](https://zerodha.com/marketintel/bulletin/461711/muhurat-trading-session-on-account-of-diwali-november-2026).
- [Zerodha closing auction operating changes](https://zerodha.com/z-connect/general/everything-you-need-to-know-about-closing-auction-session-cas).
