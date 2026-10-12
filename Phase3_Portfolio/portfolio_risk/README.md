# TradeX Portfolio Risk Model

Portfolio-level risk gate for the TradeX trading engine. Evaluates the
**entire portfolio together** (not stock-by-stock) — concentration,
sector exposure, correlation, volatility, tail risk, drawdown, liquidity,
stop-loss exposure, and stress scenarios — and produces a single
`portfolio_risk_score`, a risk classification, and an explicit
`PORTFOLIO_RISK_STATUS` decision with reasoning.

This is the last gate before `Execution / Trade Decision` in the TradeX
pipeline:

```
... -> Position Sizing -> Capital Feasibility -> Portfolio Risk -> Execution
```

## Install

No external service dependencies. Requires:
```
numpy
pandas   (not currently used directly by the risk math, reserved for future
          reporting/backtest tooling — see "Known limitations")
scipy    (only for the inverse-normal CDF in parametric VaR)
```

```bash
pip install numpy scipy --break-system-packages   # if not already present
```

## Run the example

```bash
python -m portfolio_risk.examples.example_portfolio
```

Prints a full JSON risk report for a 4-position ₹100,000 portfolio, then
runs a "what if I add this trade?" pre-check.

## Run the tests

The test suite uses `pytest`-style syntax (`pytest.approx`, `@pytest.fixture`).
If `pytest` is installed:

```bash
python -m pytest portfolio_risk/tests/ -v
```

If `pytest` is not available in your environment, a dependency-free runner
is included:

```bash
python run_tests.py
```

Both run the same 16 required scenarios (spec section 22) plus a
determinism check. All 17 currently pass.

## Minimal usage

```python
from portfolio_risk import PortfolioRiskEngine, PortfolioState, Position

positions = [
    Position(
        symbol="TCS", quantity=2, entry_price=3550, current_price=3610,
        sector="IT", stop_loss_price=3450,
        volatility=0.21, beta=0.85, avg_traded_value=8_00_00_000,
        return_history=[...],   # daily fractional returns, most-recent-last
    ),
    # ... more positions
]

portfolio = PortfolioState(
    total_capital=100_000,
    available_cash=20_000,
    positions=positions,
    peak_portfolio_value=105_000,
    current_portfolio_value=99_000,
    market_regime="NEUTRAL",
)

engine = PortfolioRiskEngine()
report = engine.assess(portfolio)   # dict — see schemas.py for the full shape

print(report["portfolio_risk_score"], report["risk_class"], report["risk_status"])
print(report["decision_explanation"])
```

### New-trade pre-check

```python
proposed = Position(symbol="WIPRO", quantity=40, entry_price=480, current_price=480,
                     sector="IT", stop_loss_price=455, is_proposed=True, ...)

impact = engine.evaluate_new_trade(portfolio, [proposed])
print(impact.before_score, "->", impact.after_score)
print(impact.new_breaches_caused_by_trade)
print(impact.final_recommendation)
```

## Documentation

- `docs/METHODOLOGY.md` — every formula used, explained and justified.
- `docs/INTEGRATION.md` — the exact input/output contract with Portfolio
  Manager, Position Sizing, Capital Feasibility, and upstream models.
- `docs/VALIDATION.md` — test results and what they demonstrate.
- `docs/LIMITATIONS.md` — explicit assumptions and known limitations.

## Package layout

```
portfolio_risk/
├── __init__.py                    public API
├── config.py                      all thresholds/weights (nothing hardcoded in engines)
├── models.py                      Position, PortfolioState, DataQualityReport
├── validation.py                  input sanitation, spec section 18
├── schemas.py                     TypedDict documentation of assess() output
├── concentration_engine.py
├── sector_risk_engine.py
├── correlation_engine.py
├── covariance_engine.py
├── downside_risk_engine.py
├── var_engine.py
├── expected_shortfall_engine.py
├── drawdown_engine.py
├── capital_utilization_engine.py
├── liquidity_risk_engine.py
├── stop_loss_risk_engine.py
├── stress_test_engine.py
├── marginal_risk_engine.py
├── risk_budget_engine.py
├── risk_limit_engine.py
├── scoring_engine.py
├── recommendation_engine.py
├── trade_impact_engine.py
├── portfolio_risk_engine.py       orchestrator / public entry point
├── tests/test_portfolio_risk.py
├── examples/example_portfolio.py
└── docs/
```
