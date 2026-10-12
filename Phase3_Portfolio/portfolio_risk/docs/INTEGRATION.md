# Integration Guide

Defines the exact contract between the Portfolio Risk Model and the rest
of the TradeX pipeline (spec section 27):

```
Portfolio Manager <-> Position Sizing <-> Capital Feasibility <-> Portfolio Risk
```

The Portfolio Risk Model is a **consumer and synthesizer** of upstream
outputs (spec section 26) — it does not recompute stock-level risk,
return, or cost figures that already exist upstream.

---

## 1. Portfolio -> Risk Model

Upstream module: **Portfolio Manager**.

Construct a `portfolio_risk.models.PortfolioState`:

| Field | Source | Notes |
|---|---|---|
| `total_capital` | Portfolio Manager / Capital Feasibility | Required. |
| `available_cash` | Portfolio Manager | Required. |
| `positions` | Portfolio Manager | List of `Position` (open) — see below. |
| `peak_portfolio_value` | Portfolio Manager | Optional but needed for drawdown. |
| `current_portfolio_value` | Portfolio Manager | Optional; falls back to `portfolio_equity()`. |
| `daily_pnl`, `cumulative_pnl` | Portfolio Manager | Currently informational only; not yet consumed by any engine (see Limitations). |
| `market_regime` | Market Regime Model | String label — see regime table in `config.py`. Unrecognized strings fall back to a conservative multiplier. |

## 2. Proposed Position -> Risk Model

Upstream modules: **Position Sizing**, **Portfolio Manager**.

Construct a `portfolio_risk.models.Position` with `is_proposed=True`
(only relevant when passed to `evaluate_new_trade`; `assess()` treats
every position in `PortfolioState.positions` with `is_proposed=False`
as "current" — proposed positions passed directly into `assess()` are
otherwise ignored by every engine, by design, so accidentally including
a proposed trade in a routine assessment cannot silently inflate the
"current" risk picture).

| Field | Source | Notes |
|---|---|---|
| `symbol`, `quantity`, `entry_price`, `current_price` | Position Sizing | Required, validated (see `validation.py`). |
| `sector`, `market_cap_class` | FILTER2.0 | Defaults to `"UNKNOWN"` sector if absent (flagged). |
| `stop_loss_price` | Position Sizing / Risk Prediction Model | Missing stop = unbounded worst case, explicitly flagged. |
| `expected_return`, `predicted_probability`, `confidence_score` | Return Prediction Model | Carried but **not used** in any risk calculation — return data never influences the risk decision (spec section 29). |
| `predicted_risk` | Risk Prediction Model | Carried through but not currently blended into the portfolio score (see Limitations — stock-level risk is implicitly captured via volatility/covariance instead). |
| `expected_net_pnl`, `transaction_cost` | Cost Model | Carried; not yet used by a rebalance-cost calculation (see Limitations, spec section 15). |
| `volatility`, `atr`, `beta` | Risk Prediction Model / market data | `volatility` is the primary fallback input when return history is insufficient for covariance estimation. |
| `avg_traded_value` | FILTER2.0 | Required for liquidity risk; without it, liquidity for that symbol is `LIMITED_DATA`. |
| `return_history` | Market data layer | Daily fractional returns, most-recent-last. Drives correlation, covariance, VaR, ES, downside risk. Below `min_history_days` (default 30), the affected engine degrades gracefully (see `docs/LIMITATIONS.md`). |

## 3. Risk Model -> Portfolio Manager

`PortfolioRiskEngine.assess(portfolio)` returns a `dict` matching
`schemas.PortfolioRiskReport`. Portfolio Manager should read, at minimum:

- `risk_status` (`ACCEPT` / `ACCEPT_WITH_WARNING` / `REDUCE_EXPOSURE` /
  `REJECT_NEW_POSITION` / `EMERGENCY_REDUCTION`) — the actionable gate.
- `risk_drivers` / `recommendations` / `decision_explanation` — human-
  readable justification to log or surface.
- `risk_limits` — per-category `PASS`/`WARNING`/`BREACH` dict, for any
  automated logic that needs a specific limit rather than the overall
  status.

## 4. Risk Model -> Position Sizing

Two integration points:

### a) `MAX_ACCEPTABLE_POSITION_SIZE` (derived, not a direct field)

The model does not currently return this as a single number (see
Limitations), but Position Sizing can derive it directly from
`risk_limits.position_concentration`'s `max` threshold together with
`portfolio_equity`:

```python
max_position_value = limits.max_position_weight * portfolio.portfolio_equity()
```

using the **regime-adjusted** limits via
`config.RiskConfig.limits_for_regime(portfolio.market_regime)`, not the
raw defaults, so the constraint is already regime-aware.

### b) `REMAINING_RISK_BUDGET`

```python
report = engine.assess(portfolio)
remaining = report["risk_budget"]["total_remaining"]          # 0-100 units
category_remaining = report["risk_budget"]["category_remaining"]  # per-category
```

Position Sizing should treat a proposed trade that would consume more
than `category_remaining["concentration"]` (etc.) of the appropriate
category's budget as a signal to shrink the trade, and should always
follow up with the trade pre-check below before finalizing size.

### c) New-trade pre-check (the primary integration point)

```python
impact = engine.evaluate_new_trade(portfolio, [proposed_position])

impact.before_score            # float, 0-100
impact.after_score              # float, 0-100
impact.score_change              # after - before
impact.new_breaches_caused_by_trade  # list[str] — empty if trade is safe
impact.risk_contribution_of_new_position_pct  # this position's share of post-trade portfolio variance
impact.final_recommendation      # human-readable string
impact.before_full_report / impact.after_full_report  # full schema dicts, for detailed diffing
```

Position Sizing should call this **before** finalizing any position size
and treat a non-empty `new_breaches_caused_by_trade` as a hard signal to
resize or reject, consistent with `PORTFOLIO_RISK_STATUS` semantics.

## 5. Risk Model -> Capital Feasibility

Capital Feasibility should read `capital.capital_utilization`,
`capital.available_cash`, and `risk_limits.cash_buffer` /
`risk_limits.invested_capital_pct` to confirm a proposed capital
commitment doesn't breach the configured minimum cash buffer — this is
a second, independent check to the concentration/position-size gate
above, since a trade can be small relative to the portfolio yet still
push total invested capital past the policy ceiling.

## 6. What the Portfolio Risk Model does NOT do

- It does not size positions. It tells Position Sizing what is/isn't
  acceptable; Position Sizing decides the actual quantity.
- It does not decide which trades to propose. It only evaluates trades
  already proposed by Portfolio Manager / Position Sizing / Opportunity
  Scoring.
- It does not recompute stock-level expected return, predicted risk, or
  transaction cost — those are consumed as given from upstream and
  never overridden.
- It does not persist state between calls. Every `assess()` call is a
  pure function of the `PortfolioState` passed in; caching/persistence
  of historical assessments for backtesting (spec section 23) is the
  caller's responsibility (see Limitations).
