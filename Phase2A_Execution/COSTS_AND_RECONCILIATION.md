# Account costs and financial reconciliation

Priority 6 adds account-specific forward allowances and a separate financial
ledger. Prices still come from confirmed fills. The engine now sizes entries
and calculates targets with dated, account-bound cost assumptions; final broker
records can then establish actual costs, holdings, cash and realized P&L.
The buy-trigger decision remains deferred.

**Implementation and synthetic checks are complete; real-account financial
acceptance is pending.** No real statements or account tariff have been supplied.
`MATCHED` means the supplied, operator-normalized records agree with the engine.
It does not authenticate those records or certify the account for live trading.

## Configure costs before the next live entry

```bash
mkdir -p state/finance
cp cost-profile.example.json state/account.cost-profile.json
```

Review the account tariff, account type, exact stocks/exchanges, applicable
brokerage and DP charges. Edit the copied profile with the account ID, a version
ID, reviewer, source references, reviewed dates and both scenarios for each
approved stock. Set `example` to `false` only after that review. In your ignored
`config.live.json`, add:

```json
"cost_profile": "state/account.cost-profile.json"
```

Paths are relative to the working directory. Date coverage uses India time;
`valid_until` is an operator review deadline, not a promise that tariffs remain
unchanged until then. Missing, expired, example, wrong-account or incomplete
profiles block **new live entries and resume**, while existing protection and
exit recovery continue using saved trade assumptions. All configured stocks
must have an explicit schedule. There is no automatic fallback to a generic
live tariff. Paper mode can still use the original `fees` settings.

The example contains a public resident-individual NSE equity tariff illustration,
not your approved account configuration. BSE rates can depend on the scrip group;
NRI, non-individual and other account plans differ. There is deliberately no BSE
default. Configure allowances for your actual supported account and instruments.

| Profile field | Unit and calculation |
|---|---|
| `brokerage_bps` | Basis points of turnover; 1 bp = 0.01%. |
| `brokerage_cap` | Rupees per filled order; `null` means no cap. |
| `brokerage_min` | Conservative per-order minimum allowance, before GST. |
| `exchange_bps`, `sebi_bps`, `ipft_bps` | Separate turnover-based charges before GST. |
| `gst_pct` | Percent on brokerage + exchange + SEBI + IPFT components. |
| `stt_buy_bps`, `stt_sell_bps` | Side-specific STT allowance. |
| `stamp_buy_bps`, `stamp_sell_bps` | Side-specific stamp-duty allowance. |
| `dp_sell` | All-in rupee allowance per possible filled sell order, including DP GST. |
| `buy_fixed`, `sell_fixed` | Additional all-in rupee allowances per filled order. |
| `rounding_buffer` | Additional rupees per order for contract-note aggregation/rounding differences. |

Every field is explicit, including zeros. Rates must be finite and nonnegative;
unknown fields are rejected. The example converts NSE IPFT of ₹0.01/crore into
**0.00001 bps**; it does not confuse that amount with the SEBI rate. Estimates
round components upward and reserve additional rounding for split exits.

### Target and quantity behavior

`DELIVERY` and `INTRADAY` are both required even for CNC. A partly intraday order
can incur brokerage on the whole order alongside delivery taxes on retained
shares. The model combines conservative component allowances across scenarios,
including brokerage and its GST, instead of assuming the more expensive complete
scenario covers every mixture. Minima and caps apply per order, not per exchange
fill. The entry budget includes the buy allowance.

The target is a tick-aligned price satisfying:

```text
sale proceeds - entry notional - buy allowance - sell allowance >= desired profit
```

Sell allowance covers up to the snapshotted `max_exit_attempts`, including a GTT
child. DP is conservatively reserved for each possible sell order even when the
broker ultimately debits it once for a scrip/day. This can put the target higher
than a single-order calculation. Explicit extra `retry-exit` attempts, tariff
changes, gaps, emergency exits and fills below the target can defeat the goal;
this is not a guaranteed realized return. The stop remains a gross price-distance
rule, not a maximum net-loss promise.

Every new trade saves the selected rates, profile hash, source reference and
exit-order allowance. Changes or removal of the file do not reprice existing
targets, block their exits, or rewrite historical estimates. Existing trades
without a cost snapshot keep their original `fees` assumptions. On closure,
`estimated_fees` and `estimated_net_pnl` remain explicitly estimated. The paper
broker's simulated cash fees still use `config.fees`; it is not a statutory
contract-note simulator. The separate Phase 2B research library is not imported
into this execution path.

## Reconcile independent broker evidence

This command needs no session token and makes **no broker requests**. Download
the final tradebook, contract notes, funds/DP ledger, holdings and P&L records from
your broker. Preserve originals in an ignored local directory. Daily order books
alone are insufficient, and a virtual contract note is still an estimate.

```bash
python -m execution --config config.live.json --db state/live.sqlite \
  finance-template --output state/finance/account.financial-evidence.json

# Normalize independent broker values into the template, preserving originals.
# Record a SHA-256 for every referenced source, for example:
sha256sum state/finance/trades.csv

# Inspect without changing the journal or entry gates:
python -m execution --config config.live.json --db state/live.sqlite \
  reconcile-finances --evidence state/finance/account.financial-evidence.json --replay

# Record an atomic audit receipt and matching per-trade financial results:
python -m execution --config config.live.json --db state/live.sqlite \
  reconcile-finances --evidence state/finance/account.financial-evidence.json

python -m execution --config config.live.json --db state/live.sqlite health
```

The template is intentionally incomplete. `engine_reference` is for joining
order identities only: never copy the engine's estimated values into broker
evidence. `--replay` never clears a live entry gate. An ordinary import records
both success and failure, so discrepancies remain visible after a restart.
Reports are saved with mode 0600 under ignored `state/finance/`; keep original
statements private too. Exit codes: 0 for `MATCHED`, 2 for a discrepancy/invalid
report, 1 for command/file/locking failures. A failed import never submits an
order or changes position quantities, target prices, stop prices or GTT ownership.

### Evidence schema 1

Evidence is **cumulative from a fixed opening baseline through `as_of`**, not a
single day's file that silently omits earlier engine orders. Use one account and
all its engine trades, including older closed trades and current open holdings.
The first financially matching report pins the opening baseline. Subsequent
reports cannot reset it or move the accepted cutoff backward. Unknown external
fills, corporate actions, non-CNC products and cross-exchange inventory transfers
need explicit investigation; this version does not silently net them into an
engine trade. Existing unrelated holdings can be carried unchanged in opening
and closing inventory. A dedicated execution account avoids ambiguous activity.

| Section | Required data |
|---|---|
| Header | `schema: 1`, `account_id`, `period_start`, `as_of`. Times are UTC epoch seconds; fill-day deduplication uses IST. |
| `sources` | Unique `id`, `kind`, relative `path`, SHA-256. Kinds: `trades`, `charges`, `holdings`, `funds`, `pnl`; all required. Paths must remain inside the evidence directory, each file ≤32 MiB. |
| `fills` | `id` (exchange trade ID), `order_id` (Kite ID), `stock` (`NSE:SYMBOL`/`BSE:SYMBOL`), `side`, positive integer `quantity`, decimal `price`, `at`, `source` (trades). Map exchange order numbers to Kite IDs from broker evidence; do not assume the two identifiers are interchangeable. |
| `charge_groups` | Unique `id`, unique `order_ids`, `sources` (charges), complete `components`. Each filled order belongs to exactly one group. |
| Charge components | `brokerage`, `stt`, `exchange`, `sebi`, `ipft`, `gst`, `stamp`, `dp`, `other`, each nonnegative whole paise. Supply explicit zeros. Positive `other` needs `other_description`. |
| `holdings` | `source` (holdings), `opening` and `closing` maps of stock to owned quantity. Include unsettled/T1 ownership correctly; do not double-subtract today's sells. |
| `cash` | `source` (funds), signed whole-paise `opening`, `closing`, `unsettled_opening`, `unsettled_closing`, and `movements`. These are ledger balances, **not available margin**. |
| Cash movements | Unique `id`, signed whole-paise `amount`, `description`, `source` (funds). Positive = credit, negative = debit. Non-trading flows only; do not repeat trade settlements or trading charges here. |
| `pnl` | `source` (pnl), signed `realized_gross`, `net_after_costs`, normalized to the definition below. No market-value/unrealized P&L. |

The normalized values are trusted operator input. The tool verifies file hashes
and calculations, but does not parse arbitrary PDFs/CSVs, authenticate the files,
or prove a normalization is faithful. Verify the account, dates and amounts
against originals. Hashes detect changed source files; they do not establish
broker authenticity. Missing or not-yet-posted fees must stay incomplete rather
than being filled with zeros to obtain a passing result.

Charge groups combine final contract-note taxes with separately posted ledger
charges such as DP **once**. Use the narrowest defensible group. Broker notes can
aggregate charges across orders, so group costs are allocated by actual turnover,
with deterministic largest remainders preserving every paise. This allocation
is labeled `GROUP_TURNOVER_LARGEST_REMAINDER`; per-trade allocated costs are not
claimed to be broker-provided per-trade charges. Sources containing both funds
and charge evidence may be referenced under separate IDs/kinds with the same hash.

Cash reconciliation uses:

```text
opening ledger cash + opening unsettled cash
+ sale notional - buy notional - actual trading costs + non-trading movements
= closing ledger cash + closing unsettled cash
```

`realized_gross` uses actual exchange fill values and the entry's weighted average
cost for the quantity sold. `net_after_costs` subtracts **all trading costs incurred
in the cumulative period**, including costs of still-open entries; it excludes
unrealized gains and non-trading cash movements. Normalize the broker P&L to that
basis explicitly, including separate DP costs. Closed trades get
`financials.actual_net_pnl`; still-open trades have `null` for that final number.
Deposits and withdrawals never increase trading P&L. The importer does not
present this trading figure as tax P&L or total account wealth.

Quantities must match exactly. Aggregate cash/P&L tolerance is ₹0.01. Order value
checks allow ₹0.005 per share for a broker's two-decimal average; actual fill
values remain the accounting basis. Duplicate IDs, missing/overlapping charge
groups, unknown components, non-finite money and wrong source roles fail closed.
Orders must have authoritative terminal states before being financially matched;
an open position with completed orders can still be reconciled.

## Daily operating gates and corrections

Before a new live entry, each filled order placed before today's IST date must
match a successful financial receipt, including its current quantity and average.
If the final broker records are unavailable, new entries remain blocked until
they are reconciled. Existing GTTs, polling, cancellations and exits continue.
Financial errors do not grant permission to cancel or resubmit an uncertain order.

| Alert | Recovery |
|---|---|
| `COST_PROFILE_REQUIRED` / `COST_PROFILE_INVALID` | Review/install an account-bound, current profile covering the whitelist. |
| `FINANCIAL_RECONCILIATION_REQUIRED` | Import cumulative final evidence covering prior-day fills and any corrections. |
| `FINANCIAL_DISCREPANCY` | Inspect report issues; correct the source/state problem, then import a matching report. |
| `COST_PROFILE_RECONCILIATION_REQUIRED` | Profile changed after a receipt; rerun reconciliation against the revised profile. |

The report compares actual charge-group totals against the current profile's
allowances only for groups whose fills fall within that profile's reviewed dates
and currently configured stocks. Historical costs outside that scope still enter
all accounting checks and appear in `cost_comparison.outside_profile_scope`;
new tariffs are not retrospectively applied to old bills. A receipt with no
comparable groups is not evidence that the current tariff has been validated.
A `COST_ALLOWANCE_SHORTFALL` blocks new entries even if the financial
figures match. Review the tariff/allowances, version the profile, then rerun; rates
are never auto-adjusted from one anomalous bill. Historical estimates remain
unchanged. Actual results from an earlier successful report retain their report
ID and cutoff if a later import fails; the latest global receipt exposes failure.
Receipts are append-only and idempotent for the same evidence/engine/profile.
Applying a receipt and its trade financials is a single SQLite transaction under
the existing process lock. An older cutoff or changed engine snapshot is rejected.

Keep the [account acceptance](ACCOUNT_ACCEPTANCE.md), session/calendar and market
data checks in place. A synthetic financial pass does not replace real fill,
settlement, authorization, connectivity or latency acceptance. No deployment or
real order is performed by this change.

## Primary references checked 7 November 2026

- [Zerodha charges](https://zerodha.com/charges/): account-specific brokerage,
  taxes, exchange-group differences, IPFT and DP amounts.
- [Partial delivery and intraday brokerage](https://support.zerodha.com/category/account-opening/resident-individual/ri-charges/articles/brokerage-charged-for-partial-delivery): mixed CNC orders and per-order brokerage.
- [DP charges](https://support.zerodha.com/category/account-opening/resident-individual/ri-charges/articles/what-do-dp-charges-mean): separate funds-ledger charges, account differences and settlement exceptions.
- [Contract-note interpretation](https://support.zerodha.com/category/console/reports/contract-note/articles/how-to-interpret-the-contract-note): trade evidence and aggregated charges.
- [Virtual contract notes](https://support.zerodha.com/category/console/reports/contract-note/articles/virtual-contract-note) and [Kite charge API](https://kite.trade/docs/connect/v3/margins/): estimates are not final posted account costs.

Tests use synthetic accounts and statements only. Run all lifecycle, fault and
financial tests with `python -m unittest discover -s tests -v`.
