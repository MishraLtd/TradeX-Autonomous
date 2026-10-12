# Phase 3 Integration — design, assumptions, and gaps

This document covers the decisions the integration layer had to make.
Each component's own DESIGN.md still owns its internal logic; nothing
here overrides it.

---

## 1. What the integration layer is allowed to do

Three rules, enforced throughout `phase3/`:

1. **It may shrink, never grow.** Phase 3 can reduce a quantity or drop a
   trade. It never raises a size above what a component permitted, and
   never overrides a component's rejection.
2. **It never re-derives an upstream number.** No re-prediction of
   return, risk or cost; no recomputation of a size, a gate, or a risk
   score. It reshapes values and sequences calls.
3. **It fails closed.** Missing price, unpriceable leg, PM
   `DECISION_UNAVAILABLE`, risk halt — all stop the affected scope
   (candidate or batch) rather than proceeding on an assumption.

---

## 2. Execution order, and why

```
construct_portfolio()                       once per run
    |
assess(current portfolio)                   once per run — the pre-trade picture
    |
for each selected candidate, in PM priority order:
    |
    risk_status -> risk_multiplier / halt / max position value
    calculate_position_size()
    evaluate_trade()                        at the sized quantity
        |  feasible < requested?
        +-> re-size under the verified cap, evaluate_trade() again
    evaluate_new_trade()                    at the final quantity
        |  new breach?
        +-> retry at 50%, then 25%, re-priced and re-gated each time
    commit to ledger; the post-trade report becomes the next candidate's
    pre-trade picture
```

**Why sequential rather than batch-optimal.** A simultaneous optimiser
over all candidates would allocate better on paper, but none of the four
components exposes a batch interface, and faking one would mean
reimplementing their logic here — a direct violation of rule 2. Sequential
allocation in PM's own priority order uses each component exactly as it
was specified, and PM has already done the cross-candidate work
(correlation, sector overlap, redundancy) that a batch optimiser would
duplicate.

**Why the sizing ↔ feasibility loop exists.** Truncating a risk-derived
size to whatever cash allows produces a position whose risk was never
evaluated at that size. Feeding the verified affordability figure back
into Position Sizing (the `CapitalFeasibilityConstraint` shape that
`capital_feasibility/adapters/position_sizing_adapter.py` already emits,
and that `position_sizing/constraints.py` already consumes) lets the
component that owns sizing decide the smaller size. Default is one
feedback iteration; feasibility is monotone in quantity, so further
iterations rarely change anything. `max_sizing_iterations = 1` disables
the loop and simply truncates.

**Why risk is checked twice.** The pre-trade check sets the sizing
multiplier and the concentration cap (portfolio_risk INTEGRATION.md §4a,
§4b). The post-trade check answers a question the pre-trade check
structurally cannot: does *this specific trade at this specific size*
introduce a breach. Both are the risk model's own calls — `assess` and
`evaluate_new_trade` — not a reimplementation.

**Why a risk-driven downsize is re-priced.** A smaller quantity is a
different amount of money, and costs are not linear in quantity (fixed
brokerage caps). Assuming the smaller size is affordable because the
larger one was would be an unverified inference.

---

## 3. Decisions a reviewer should push on

These are judgement calls, not derivations. Each is a single config value.

| Decision | Default | Reasoning | Challenge it if |
|---|---|---|---|
| `risk_status_multipliers` | ACCEPT 1.0, WARNING 0.75, REDUCE 0.5, REJECT/EMERGENCY halt | Monotone and conservative; the risk model states a status, not a multiplier, so the mapping had to be invented somewhere | You'd rather derive the multiplier from `risk_budget.total_remaining` continuously |
| `risk_downsize_steps` | (0.5, 0.25) | Two retries bound the work; halving is the crudest defensible step | You'd prefer a binary search for the largest non-breaching size |
| `apply_risk_position_cap` | on | Enforces concentration *before* sizing rather than rejecting after | You want the post-trade check to be the only concentration authority |
| `allow_partial_fill` | on | At ₹1,000 a partial position is usually better than none | Your execution costs make sub-scale fills uneconomic |
| `reject_on_score_jump` | off | `trade_impact_engine` already flags a big jump as CAUTION; making it a hard reject is a policy choice | You want a hard ceiling on score movement |
| `directional_regime_confidence` | 0.65 | Below it, PM's `TRENDING` is treated as `SIDEWAYS` for sizing | You have a directional regime signal to pass explicitly |
| `candidate_cost_basis` | `PER_SHARE` | `position_sizing/DESIGN.md` Part 4/12 expects per-share; `PortfolioCandidate.expected_total_cost` doesn't state its basis | Your Opportunity Scoring emits a whole-trade figure — set `PER_TRADE`, and Phase 3 will leave the field unset rather than guess a reference size |

---

## 4. Assumptions, stated plainly

1. **Long-only, entry leg only.** Every instruction is a BUY.
   `portfolio_risk` supports SHORT and `capital_feasibility` evaluates the
   opening leg by design, but nothing here constructs a short. Exits are
   the Position Manager's job, not Phase 3's.
2. **One reference price per opportunity.** Sizing, feasibility and risk
   all use `ExecutionInput.entry_price`. Slippage between decision and
   fill is the Execution Engine's concern; Phase 3 does not model a fill
   distribution.
3. **A proposed position is marked at its entry price.** No fabricated
   unrealized P&L enters the portfolio the risk model sees.
4. **`committed_capital` unknown ≠ zero.** Absent, it's treated as 0 for
   arithmetic but flagged `committed_capital_is_known=False` on the
   capital state and surfaced as a run warning.
5. **Per-share edges come from the trade plan, not the candidate.**
   `expected_upside` = target − entry, `expected_downside` = entry − stop.
   `PortfolioCandidate.expected_net_profit` is a whole-trade figure at an
   unknown size and `expected_downside` is fractional, so neither can be
   converted to per-share currency without inventing the basis.
6. **Existing positions get risk data only if you supply it.**
   `Phase3Request.open_position_inputs` carries return history, volatility
   and traded value for open positions. Without it, the risk engines
   degrade and flag it themselves — Phase 3 imputes nothing.
7. **PM priority order = contribution score descending.** Ties fall back
   to PM's own list order, so the run is deterministic.

---

## 5. Known gaps

* **No batch-level optimisation.** First candidate in priority order gets
  first call on capital. A high-scoring expensive name can crowd out two
  cheaper ones with better combined risk-adjusted value.
* **Pre-trade risk picture is one step stale within a batch.** Each
  candidate is judged against the post-trade report of the previous one,
  which is correct, but the sizing multiplier for candidate N is derived
  from a report computed before candidate N's own size was known.
* **Exits and rebalancing.** PM's `existing_position_actions` are passed
  through on the result untouched. Acting on `EXIT_REVIEW` or
  `REPLACE_CANDIDATE` — including freeing the capital an exit would
  release *before* funding a replacement — is not implemented.
* **No intraday/delivery capital segregation.** Zerodha treats MIS margin
  and CNC differently; Capital Feasibility's gates are product-agnostic
  and so is this layer.
* **Approximate cost provider is not an authority.** Its rates are
  defaults that change by regulation and broker. Replace it with
  `CostModelProvider` before anything touches real money.
* **Correlation provider is pass-through.** `run()` accepts one and hands
  it to PM; Phase 3 has no correlation model of its own.
* **Single portfolio, single run.** No concurrency control. Two
  simultaneous runs against the same account would each see the other's
  capital as available.

---

## 6. Component contracts used

| From | Called | Used for |
|---|---|---|
| portfolio_manager | `construct_portfolio()` | selection + priority + existing-position actions |
| position_sizing | `calculate_position_size()` | quantity, with risk/capital/PM constraints |
| capital_feasibility | `engine.evaluate_trade()` | gates + feasible/max quantity (pure core, via `price_fn`) |
| capital_feasibility | `adapters.to_position_sizing_constraint()` | closing the size ↔ afford loop |
| capital_feasibility | `capital_state.build_capital_state()` | reserve-aware deployable capital |
| portfolio_risk | `PortfolioRiskEngine.assess()` | pre-trade status, budget, limits |
| portfolio_risk | `PortfolioRiskEngine.evaluate_new_trade()` | post-trade breach check |
| portfolio_risk | `config.limits_for_regime()` | regime-adjusted concentration cap |

`capital_feasibility.api.evaluate()` is deliberately **not** used: it
hard-wires the Cost Model and reads exposure from `PortfolioState` alone,
which cannot see within-batch allocations. Phase 3 calls the pure core
with its own ledger-aware exposure instead.
