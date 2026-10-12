# TradeX Autonomous

Private extraction of the TradeX Phase 2 and Phase 3 components into a simpler phase-oriented repository. The original `MishraLtd/TradeX` repository is the source of truth and must remain unchanged during this extraction.

## System boundaries

- **Fixed stock universe:** the operator supplies the approved stock list. The runtime does not add securities on its own.
- **Phase 1 — Pre-training:** intentionally not extracted as a completed phase; it will be designed and completed separately.
- **Phase 2 — Monitoring and inference:** existing Return, Risk, Market Regime, and Opportunity models.
- **Phase 2A — Monitoring, decisions, execution:** the broker execution placeholder is intentionally retained until the integrated engine is backtested and accepted.
- **Phase 2B — Cost and P&L:** existing cost model, fee calculations, execution-friction estimates, break-even logic and reconciliation.
- **Phase 3 — Portfolio manager:** portfolio selection, position sizing, capital feasibility and portfolio risk.

## Important status

This repository is an extraction and integration workspace; it is not yet approved for live trading.

Three readiness workstreams remain to be completed separately:
1. Replace the Return Model's synthetic example data with validated real historical data and production data interfaces.
2. Design, train, validate and integrate the Risk Model's fitted ML model; the current documented inference baseline is Monte Carlo.
3. Run integrated regression/backtests and paper-trading validation for Phase 3 and the end-to-end engine.

The broker placeholder remains by design. The Kite broker adapter will only replace it after the engine has met agreed backtest and paper-trading acceptance criteria.

## Initial extraction structure

```
Phase2_Monitoring/
  return_model/
  risk_model/
  market_regime/
  opportunity_model/
Phase2A_Execution/
  README.md              # intentional placeholder / integration boundary
Phase2B_CostPnL/
  cost_model/
  docs/
  examples/
  tests/
Phase3_Portfolio/
  portfolio_manager/
  position_sizing/
  capital_feasibility/
  portfolio_risk/
  phase3/
  cost_model/            # matching copy retained for existing import compatibility
  tests/
  vendor_tests/
  docs/
```

Existing package contents, configuration, tests, database schemas, examples and docs are to be preserved. Any path/import compatibility work is separate from model logic changes.

## Safety

Autonomous order placement must never bypass hard portfolio/capital limits, input validation, kill-switch handling or broker-order state checks. Passing unit tests alone is not evidence of profitability. Use backtesting and paper trading before live capital.

## Source

Original repository: https://github.com/MishraLtd/TradeX
