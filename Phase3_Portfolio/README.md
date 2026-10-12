# TradeX Phase 3 — Capital Management, integrated

Four independently-specified components, wired into one callable module:

```
Portfolio Manager  ->  Position Sizing  ->  Capital Feasibility  ->  Portfolio Risk  ->  TradeInstruction[]
   (what to take)      (how much ideally)    (what we can afford)     (what it does        -> Execution Engine
                              ^                        |               to the book)
                              +------------------------+
                        verified affordability re-sizes the trade
```

## Layout

```
phase3/                 the integration layer (the new code)
  pipeline.py           orchestration: Phase3Engine.run()
  adapters.py           every type/numeric boundary crossing
  ledger.py             within-batch capital + exposure accounting
  costs.py              Cost Model boundary (+ an approximate stand-in)
  regime.py             the three regime vocabularies, reconciled
  config.py             orchestration policy + the four component configs
  models.py             ExecutionInput / Phase3Request / TradeInstruction / Phase3Result
  enums.py              Phase3Status / Stage / AllocationStatus
  serialization.py      JSON-safe projection for the Trade Journal

portfolio_manager/      component 1  (unmodified)
position_sizing/        component 2  (unmodified)
capital_feasibility/    component 3  (unmodified)
portfolio_risk/         component 4  (unmodified)
cost_model/             upstream cost model (unmodified)

tests/                  integration tests (44, incl. 10 on the real Cost Model)
vendor_tests/           each component's original suite, moved intact
demo_phase3.py          runnable end-to-end example at ~Rs 1,000 capital
run_tests.py            runs everything (works with or without pytest)
schema.sql              persistence for runs, instructions and outcomes
docs/INTEGRATION.md     design decisions, assumptions, and known gaps
docs/VALIDATION.md      test results and what the real Cost Model revealed
```

None of the four component packages were edited. Everything the wiring
needed lives in `phase3/`.

## Usage

```python
from decimal import Decimal
from phase3 import Phase3Engine, Phase3Request, ExecutionInput
from phase3.costs import CostModelProvider

engine = Phase3Engine(cost_provider=CostModelProvider(cost_engine, cost_models))

result = engine.run(Phase3Request(
    portfolio_state=portfolio_state,       # portfolio_manager.PortfolioState
    candidates=candidates,                 # List[PortfolioCandidate] from Opportunity Scoring
    market_context=market_context,
    execution_inputs={                     # prices/stops/liquidity per opportunity
        "OPP-1": ExecutionInput(
            opportunity_id="OPP-1",
            entry_price=Decimal("62.40"),
            stop_loss=Decimal("59.90"),
            target=Decimal("67.50"),
            atr_pct=Decimal("0.016"),
            average_traded_value=Decimal("1450000000"),
            return_history=daily_returns,   # drives correlation/VaR
            lot_size=1,
        ),
    },
    committed_capital=Decimal("0"),         # capital already earmarked today
))

for instruction in result.instructions:     # -> Execution Engine -> Zerodha Kite
    place_order(instruction.symbol, instruction.quantity, instruction.entry_price)
```

`result.outcomes` has one record per selected candidate — allocated or
not — carrying the real `PositionSizeResult`, `CapitalFeasibilityResult`
and `TradeImpactResult` behind it, so every rejection is traceable to the
component that made it.

## Running it

```bash
python demo_phase3.py        # end-to-end example
python run_tests.py          # all suites
python run_tests.py phase3   # integration tests only
```

`run_tests.py` uses pytest when installed and falls back to a built-in
runner when it isn't. Current state: **177 passed, 0 failed, 1 skipped**
(the skip is a parametrized case the fallback runner doesn't implement;
it runs under real pytest).

## Cost Model

Capital Feasibility takes a `price_fn` callback rather than importing the
Cost Model, and `phase3/costs.py` is where that callback is supplied:

* `CostModelProvider(cost_engine, cost_models)` — the real thing. Use it.
  `demo_phase3.py` picks this automatically when `cost_model` imports.
* `ApproximateZerodhaCostProvider()` — a labelled stand-in for running
  Phase 3 without the Cost Model. Every figure it produces is stamped
  `APPROX-…`, and it understates real charges by 40–70% (see
  docs/VALIDATION.md). It is not an authority on charges.

**Always pass the liquidity snapshot.** The real Cost Model widens its
spread/impact assumption when it has none, pricing the same trade ~35%
higher. The pipeline passes it on every call; anything calling the
provider directly must too.

Both fail closed: an unpriceable leg raises, and a raise abandons the
whole batch rather than emitting instructions funded by a guess.

See `docs/INTEGRATION.md` for the decisions behind all of this, the
assumptions that are open to challenge, and what is deliberately not
handled yet.
