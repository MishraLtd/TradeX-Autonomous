# Phase 2B — Cost and P&L

Contains the canonical Phase 2 TradeX Cost Model, including per-leg fee calculations, slippage, market impact, break-even analysis, scenarios, examples, tests, and reconciliation. Phase 3 retains its own identical cost_model copy because existing Phase 3 imports depend on that package location. The two source copies had identical Git blob SHAs for all 17 Python package files when checked.

Do not use the approximate Phase 3 cost provider for a trading viability decision; follow the Cost Model and Phase 3 validation documentation. Estimated P&L and realised P&L should be reported separately.
