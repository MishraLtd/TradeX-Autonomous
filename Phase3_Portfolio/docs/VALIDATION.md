# Validation — Phase 3 against the real Cost Model

All figures below come from running the repo as shipped:
`python run_tests.py` and `python demo_phase3.py`.

## Test results

```
phase3               44 passed     integration layer, incl. 10 real-Cost-Model tests
position_sizing      28 passed, 1 skipped
capital_feasibility  54 passed     previously unrunnable — needed cost_model
portfolio_manager    23 passed
portfolio_risk       17 passed
cost_model           11 passed
-------------------------------------------------
                    177 passed, 0 failed, 1 skipped
```

The one skip is `test_capital_tiers_produce_valid_result`, a
`@pytest.mark.parametrize` case the offline fallback runner does not
implement. It runs normally under real pytest.

## What the real Cost Model confirmed

**The monotonicity assumption holds.** `capital_feasibility/max_quantity.py`
binary-searches over quantity and documents (spec §33) that
`required_capital(qty)` must be non-decreasing. That was previously
asserted against an approximation that was monotone by construction, so
it proved nothing. Checked against real charges over qty 1–400 for both
DELIVERY and INTRADAY — including across the intraday brokerage cap,
the most likely place for it to break — it holds. The binary search is
sound.

**The whole capital_feasibility suite now runs.** All 54 tests, including
the ones that exercise the real Cost Model bridge (`test_provider_integration`,
`test_engine_integration`, `test_case15_missing_cost_model_output_fails_closed`)
and the max-quantity search against real pricing.

## What the real Cost Model changed

**Cost is liquidity-dependent, so the liquidity snapshot is a required
input, not an enrichment.** Priced without a `LiquiditySnapshot`, the
real model widens its spread/impact assumption:

| 7 × TESTCO @ ₹50, NSE delivery | entry cost |
|---|---|
| with liquidity data | ₹0.95 |
| without | ₹1.28 (+35%) |

A trade priced without the snapshot looks more expensive and can be
rejected on capital grounds it would actually clear. The pipeline passes
the snapshot on every pricing call, including the final one that stamps
the instruction, and `test_missing_liquidity_data_prices_more_conservatively`
pins that behaviour. This surfaced as a genuine test failure first — an
early reproducibility test re-priced an instruction without the snapshot
and got a different number.

**The approximate stand-in was understating costs by ~40–70%.**

| qty × ₹62.40, NSE delivery | real | approximate |
|---|---|---|
| 1 | ₹0.22 | ₹0.14 |
| 10 | ₹2.29 | ₹1.36 |
| 50 | ₹11.51 | ₹6.82 |

Direction matters more than magnitude: the stand-in is cheaper, so it
would have let a trade look affordable that the real schedule rejects.
`test_real_costs_exceed_the_approximate_stand_in` asserts the ordering
holds, so the stand-in can never silently become the optimistic one.

At the demo's ₹1,000 scale the switch moved total committed capital from
₹272.79 to ₹272.92 and changed no quantities — costs at this size are
dominated by position value, not charges.

## The `api.evaluate()` bypass, demonstrated

`test_shipped_api_cannot_see_within_batch_exposure` builds the concrete
case rather than asserting the claim.

Two candidates in sector TECH, ₹1,000 account. Trade 1 (₹350) is
allocated but not yet filled, so it is not in
`PortfolioState.open_positions`. Evaluating trade 2:

| exposure source | sector value seen | max feasible qty | post-trade sector ratio |
|---|---|---|---|
| `asset_exposure_from_portfolio` (what `api.evaluate()` uses) | ₹0 | higher | lower |
| `phase3.adapters.asset_exposure` (ledger-aware) | ₹350 | lower | higher |

The shipped API sees an empty sector and lets trade 2 through at a size
that puts the book over the 60% sector limit. Two further tests cover the
same failure mode through the whole pipeline: five same-sector candidates
stay inside the limit in aggregate, and any trade the within-batch sector
gate cuts is reported as `ALLOCATED_REDUCED` with
`reduced_by_stage=CAPITAL_FEASIBILITY` rather than silently shrunk.

This is a gap in `capital_feasibility`, not in the pipeline. The pipeline
works around it by calling the pure core (`engine.evaluate_trade`) with
its own exposure. The durable fix is to let
`adapters/portfolio_adapter.asset_exposure_from_portfolio` accept
pending/in-flight exposure, the same way `account_state_from_portfolio`
already accepts `committed_capital` for the cash side of exactly this
problem. The asymmetry looks like an oversight: cash-side within-batch
accounting was designed for, exposure-side was not.
