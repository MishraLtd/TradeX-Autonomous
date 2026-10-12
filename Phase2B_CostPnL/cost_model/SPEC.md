# TradeX Cost Model — Specification & Implementation

**Scope:** Equity Delivery (CNC) and Equity Intraday (MIS), NSE + BSE, Zerodha Kite,
starting capital ≈ ₹1,000. Rates verified live against `zerodha.com/charges`
in September 2026 (see `cost_model/fee_schedule.py` header for the full
source list). F&O/currency/commodity are explicitly out of scope (PART 24).

This is a **defensive** component. Its only job is to answer, honestly and
conservatively: *does this predicted opportunity survive contact with real
costs?* It is designed to say **NO** often, at ₹1,000 capital — and the
capital-sweep example below shows it doing exactly that.

---

## 1. Executive architecture

```
TradeInput
  → validator.py            (fail closed on bad/missing/impossible data)
  → fee_schedule.py          (time-effective, sourced rate lookup)
  → calculators.py            (brokerage / STT / exchange / IPFT / SEBI / GST / stamp / DP,
                                 computed per LEG — buy and sell separately, never a
                                 single blended round-trip formula)
  → slippage.py + market_impact.py   (tiered, never-zero execution friction —
                                        spread / slippage / impact kept as three
                                        non-overlapping components)
  → engine.py                (composes all of the above into one CostBreakdown)
  → breakeven.py              (bisection solve, reusing the SAME leg calculators)
  → economics.py               (minimum required return + PASS/REJECT gate)
  → scenarios.py                (OPTIMISTIC / BASE / CONSERVATIVE)
  → ml_interface.py / portfolio_interface.py   (typed bridges for other TradeX components)
  → reconciliation.py            (predicted vs. actual contract-note diffing)
  → operating_costs.py            (infra cost — kept OUT of trade P&L, PART 16)
```

Every arrow is a separate, independently unit-tested module (see
`tests/test_cost_model.py`, 36 tests). `engine.py` contains **no rate
numbers and no thresholds** — it only composes other modules, so a rate
change or a threshold change never requires touching the orchestration
logic.

## 2. Mathematical formulas (as implemented, Sept-2026 NSE rates)

For a leg of turnover `T = price × qty`, all amounts rounded to the
paisa (`ROUND_HALF_UP`) **per leg**, matching how a real contract note is
generated per executed order:

```
brokerage   = 0                                    (delivery)
brokerage   = min(0.03% × T, ₹20)                   (intraday)
STT         = 0.10% × T           both sides         (delivery)
STT         = 0.025% × T          sell side only     (intraday)
exchange    = 0.00307% × T  (NSE) | 0.00375% × T (BSE)     both sides
IPFT        = 0.0001% × T (NSE only)                        both sides
SEBI        = 0.0001% × T  (= ₹10/crore)                    both sides
GST         = 18% × (brokerage + SEBI + exchange + IPFT)
stamp duty  = 0.015% × T  (delivery) | 0.003% × T (intraday)   BUY side only
DP charge   = ₹15.34 flat, per scrip per day               SELL side,
                                                     delivery, from demat holdings only
```

Round trip = one BUY-leg calculation + one SELL-leg calculation, summed.
This is **not** `rate × 2 × T` — several charges are side-specific
(STT-intraday, stamp duty, DP), so a blended formula would misprice every
intraday and every delivery-sell trade (PART 8).

## 3. Complete fee taxonomy (PART 1)

| Class | Charges | Who sets it |
|---|---|---|
| A. Broker-specific | Brokerage, DP charge, Call & Trade fee, AMC | Zerodha |
| B. Exchange | Transaction/turnover charge, IPFT | NSE / BSE |
| C. Statutory taxes | STT, GST, Stamp duty | GoI / State Govt |
| D. Depository | DP charge (CDSL pass-through inside Zerodha's ₹15.34) | CDSL/Zerodha |
| E. Execution/friction | Slippage, spread cost, market impact, partial-fill effects | Market microstructure |
| F. Internal/infra | Kite Connect subscription, VM, DB, monitoring | TradeX itself |

Classes A–D live in `fee_schedule.py`/`calculators.py`. Class E lives in
`slippage.py`/`market_impact.py`. Class F is deliberately isolated in
`operating_costs.py` and **never** enters `CostBreakdown.total_cost`
(PART 16 — do not distort trading P&L with business overhead).

## 4. Data model — PART 5 / PART 21

Implemented as typed dataclasses (`models.py`) rather than DB tables for
this ₹1,000-capital build (PART 24: don't build institutional infra you
don't need yet), but each maps 1:1 onto the tables PART 21 asks for:

| Requested table | Implementation |
|---|---|
| `fee_schedule` / `fee_schedule_versions` | `fee_schedule.FeeScheduleVersion`, versioned list, never mutated in place |
| `broker_rules` / `exchange_rules` / `statutory_rates` | fields on `FeeScheduleVersion` + `RateFact` (each carries `source`, `source_url`, `calculation_rule`) |
| `cost_calculation` / `cost_calculation_components` | `CostBreakdown` / `CostComponent` |
| `slippage_assumptions` / `execution_friction` | constants + tier logic in `slippage.py` / `market_impact.py` |
| `cost_reconciliation` | `reconciliation.ReconciliationReport` |

When TradeX moves to Postgres/Supabase, each dataclass becomes a table
with `effective_from`/`effective_to`/`source`/`source_url` columns
already present on `FeeScheduleVersion` — no redesign needed, just a
persistence layer.

## 5. PART 2 / PART 8 — low-capital economics (measured, not asserted)

Running `examples/example_run.py`'s capital sweep (fixed 1.5% predicted
move, NSE delivery) produces:

```
   Capital |  Qty |  Cost/Capital % | Net Return % | Viability
      ₹500 |    2 |           3.80% |       -2.30% | REJECT
      ₹750 |    3 |           2.77% |       -1.27% | REJECT
     ₹1000 |    4 |           2.26% |       -0.76% | REJECT
     ₹1500 |    6 |           1.75% |       -0.25% | REJECT
     ₹2000 |    8 |           1.49% |        0.01% | REJECT
     ₹5000 |   20 |           1.04% |        0.46% | REJECT
    ₹10000 |   40 |           0.88% |        0.62% | REJECT
    ₹25000 |  100 |           0.79% |        0.71% | REJECT
    ₹50000 |  200 |           0.76% |        0.74% | REJECT
   ₹100000 |  400 |           0.76% |        0.74% | REJECT
```

**This is the model working as intended (PART 2's explicit goal):** a
predicted +1.5% move is **rejected at every capital level up to ₹100,000**
for this example, because the **flat ₹15.34 DP charge alone is ~1.5% of a
₹1,000 position** — a fixed per-scrip cost that a percentage-based mental
model completely misses. The lesson for TradeX: at low capital, **prefer
INTRADAY over DELIVERY** (no DP charge) or **trade names where a 1.5%
predicted move is a low-confidence edge to begin with** — the Cost Model
surfaces this trade-off instead of hiding it. A full report for a larger,
more realistic edge (+3.2%, ₹250 stock) is in section 9 below and *does*
pass.

Key structural finding: **DP charges are the dominant fixed cost at
₹1,000 capital for delivery trades** — larger than brokerage (₹0),
comparable to or larger than STT. **Intraday has no DP charge** but pays
brokerage (capped ₹20) and a smaller STT — the model lets you compare
both paths directly (`TradeType.DELIVERY` vs `TradeType.INTRADAY`).

## 6. Slippage methodology (PART 6)

Three **non-overlapping** components to avoid double-counting (see
`slippage.py` module docstring):

- **spread_cost** — half the live quoted spread, priced directly when a
  quote is available; ₹0 (folded into a higher slippage assumption)
  otherwise.
- **slippage_cost** — a residual "everything else" friction: 2bps when a
  live quote exists, 15bps (market) / 5bps (limit) when it doesn't, +5bps
  for intraday urgency. **Never zero**, floored at 2bps absolute.
- **market_impact_cost** — square-root participation model,
  `impact_bps = K × vol_multiplier × sqrt(order_value / ADV)`, K=40bps,
  conservative flat 10bps fallback when ADV is unknown. Floored at
  0.5bps, capped at 250bps (flag for manual review beyond that).

LIMIT orders get a lower base assumption than MARKET orders but are
**never** assumed to fill at exactly the limit price with zero friction.

## 7. Break-even mathematics (PART 9)

Rather than a hand-derived closed-form (which risks drifting out of sync
with the real calculators — PART 25 self-critique #2), `breakeven.py`
**bisection-solves** for the exit price at which
`net_pnl(exit_price) == target`, calling the exact same per-leg
calculators used everywhere else. Example (Entry ₹100, Qty 5, Capital
₹500, delivery):

```
Net P&L target ₹  0 -> exit price ₹103.30
Net P&L target ₹  1 -> exit price ₹103.50
Net P&L target ₹  5 -> exit price ₹104.30
Net P&L target ₹ 10 -> exit price ₹105.30
Net P&L target ₹ 20 -> exit price ₹107.30
```

At ₹500 capital, the stock must move **+3.3%** just to break even on a
delivery round-trip — almost entirely the ₹15.34 DP charge. This is the
single most important number this build produces for a ₹500–1,000
account.

## 8. Backtesting integration (PART 13)

`fee_schedule.get_schedule(exchange, trade_type, as_of)` takes an
explicit `as_of` date and looks up the schedule version effective on
that date — **never "today's" schedule for a historical trade**. New
rate changes are added as **new, dated entries**; existing entries are
never mutated, so a backtest run today reproduces identical numbers next
year even after rates change (no look-ahead bias, PART 13/25 #3). A
backtest that constructs `TradeInput`s directly from historical
bar/quote data and calls `CostEngine.price_trade()` per simulated trade
is, by construction, "backtest WITH cost model" — there is no code path
that skips it.

## 9. ML interface (PART 11)

`ml_interface.evaluate_trade_economics(engine, trade, ModelPrediction)`
always returns a `cost_adjusted_score = net_return_pct × probability_of_profit`
— gross predicted return never reaches a ranking step un-costed. Example
report for a real (larger) edge:

```
=== TRADE ECONOMIC REPORT ===
Symbol: DEMO   Trade Type: DELIVERY
Entry: ₹250.00   Expected Exit: ₹258.00
Quantity: 4   Capital Used: ₹1000.00
Expected Gross P&L: ₹32.00
Brokerage: ₹0.00 | STT: ₹2.03 | Exchange: ₹0.06 | SEBI: ₹0.00
GST: ₹0.02 | Stamp Duty: ₹0.15 | DP Charges: ₹15.34
Slippage: ₹0.41 | Spread: ₹0.81 | Market Impact: ₹0.10
TOTAL EXPECTED COST: ₹18.92
EXPECTED NET P&L: ₹13.08   NET RETURN: 1.31%
BREAK-EVEN MOVE: 1.76%  (price ₹254.40)
COST / GROSS PROFIT: 59.1%
Optimistic ₹13.75 | Base ₹13.08 | Conservative ₹11.76
ECONOMIC DECISION: REJECT
  - cost_ratio 59.1% of gross profit > maximum_allowed_cost_ratio 35.0%
  - break_even_move 1.76% > expected_realistic_move 1.20%
```

Even a +3.2% predicted move on a real ₹1,000 position is rejected here —
the DP charge again consumes most of the edge relative to the gate's
35%-cost-ratio ceiling. This is the gate doing its job, not a bug: the
founder should read this as "at ₹1,000 capital, delivery trades need a
materially larger predicted edge than intuition suggests, or capital
needs to scale up, or the strategy should default to intraday."

## 10. Portfolio Manager interface (PART 12)

`portfolio_interface.capital_efficiency()` returns
`net_profit_per_rupee_committed` and (when the Risk Engine supplies a
stop-loss-implied risk figure) `net_profit_per_rupee_risked` —
deliberately requires the Portfolio Manager to compare trades on
*efficiency*, not on raw predicted-return rank.

## 11. Testing plan (PART 19) — implemented

36 tests in `tests/test_cost_model.py`, covering: ₹500/₹1,000/₹2,000/
₹10,000 trades, one-share and multi-share trades, NSE and BSE, buy-only
and sell-only and round-trip, delivery-with-DP vs intraday-no-DP,
limit vs market order slippage, high-spread / low-liquidity /
high-liquidity / high-volatility environments, missing and outdated fee
schedules (both raise `CostModelDataIncomplete`), invalid inputs
(negative/zero quantity, negative price), GST cross-checked against
Zerodha's own published worked example, stamp-duty buy-side-only
behaviour, break-even self-consistency (net P&L at the solved break-even
price ≈ ₹0), scenario monotonicity (conservative never cheaper than
optimistic), the economic gate rejecting a trade with a positive
predicted move, and the ML/Portfolio interfaces.

## 12. Real-Zerodha reconciliation plan (PART 20)

```
Predicted Cost (CostBreakdown) → actual charges read off a real
Zerodha contract note / funds statement → reconciliation.reconcile()
→ component-level diff + notes → human-reviewed model adjustment:
    - a statutory/broker rate changed  -> new FeeScheduleVersion entry
    - a systematic slippage/impact bias -> update LEVEL_4 calibrated_bps
      fed into slippage.estimate_slippage(calibrated_bps=...)
```
`reconcile()` never auto-writes to the rate registry — see its
docstring for why (avoiding silent corruption of sourced rates).

## 13. Risks and failure modes — PART 25 self-critique, answered

1. **Where could this underestimate real costs?** If Zerodha's own rate
   card changes mid-session and `fee_schedule.py` isn't updated in time —
   mitigated by `StaleFeeScheduleError` refusing to guess past a known
   `effective_to`, but there is currently no *automatic* refresh; this is
   a manual-review dependency the founder must operationalise (e.g. a
   monthly cron that diffs zerodha.com/charges against the registry).
2. **Where could it double-count?** Addressed structurally: spread,
   slippage, and market impact are defined as non-overlapping components
   (module docstrings explain the boundary); GST is computed once, on
   the already-rounded upstream components, matching Zerodha's own
   contract-note construction.
3. **Where could it use future information?** `get_schedule(as_of=...)`
   forces every calculation to state which date's rates it's using;
   backtests must pass the historical trade date, not `date.today()`.
   Liquidity/volatility snapshots passed into `TradeInput.liquidity` are
   the caller's responsibility to source from data available *as of* the
   trade — the Cost Model has no way to detect a look-ahead leak in
   caller-supplied liquidity data and this should be a Backtester-side
   invariant, documented here as a boundary of this component's guarantee.
4. **What happens at ₹1,000 capital?** See section 5 — the model
   correctly rejects most small predicted edges, dominated by the flat
   ₹15.34 DP charge on delivery trades.
5. **Which trades get rejected purely on economics?** Any trade where
   cost/gross-profit > 35%, net return ≤ 0.50%, net profit ≤ ₹2, break-even
   move > 1.20%, or position value < ₹300 (see `config.py` for the
   reasoning behind each threshold).
6. **What happens when fee rules change?** A new `FeeScheduleVersion` is
   appended (never mutating history); backtests before the change date
   continue to use the old version automatically.
7. **What happens when liquidity collapses?** `market_impact.py` uses
   participation-rate-vs-ADV; if ADV data is stale/missing the model
   falls back to a flat conservative 10bps rather than assuming zero
   impact — but a *sudden intraday* liquidity collapse not yet reflected
   in a trailing ADV figure is a real blind spot; the `high_uncertainty_tiers`
   flag in `config.GateConfig` exists precisely so a caller can choose to
   hard-reject on `NO_LIQUIDITY_DATA_FALLBACK` in stressed conditions.
8. **What happens when actual execution differs from expected?**
   `reconciliation.py` exists exactly for this — feed real contract-note
   figures back in, get a component-level diff, and recalibrate
   `slippage_bps` (LEVEL_4) accordingly over time.

## 14. Deployment notes

- Pure Python, stdlib only (`decimal`, `dataclasses`, `enum`) — no new
  dependencies for TradeX to manage.
- Drop `cost_model/` into the TradeX codebase alongside `FILTER2.0`;
  nothing here touches the filtering system.
- Kite Connect API cost: Zerodha's own charges page lists the **Personal
  tier as free** for a single-account personal deployment — set
  `OperatingCostConfig.monthly_kite_connect_cost = 0` unless/until TradeX
  moves to the commercial Connect tier (₹500/month), and treat that as a
  deliberate config change, not a default assumption (`operating_costs.py`).
- Run `python3 -m unittest tests.test_cost_model -v` from the package
  root before every deploy; run `python3 examples/example_run.py` to
  eyeball a live capital sweep and full trade report.
