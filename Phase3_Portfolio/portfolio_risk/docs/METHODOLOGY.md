# Methodology

Every formula used in the Portfolio Risk Model, why it was chosen, and
where in the code it lives.

---

## 1. Position & sector concentration

**Weight of position i:** `w_i = market_value_i / portfolio_equity`
where `portfolio_equity = available_cash + sum(open position market values)`.

**HHI (Herfindahl-Hirschman Index):** `HHI = sum(w_i^2)`.
Ranges from `1/N` (perfectly equal-weighted, N positions) to `1.0`
(single position). Standard concentration measure borrowed from
antitrust economics; used here because it penalizes concentration
non-linearly — two positions at 50% each score much worse than five at
20% each, which a simple "top-1 weight" metric alone would not capture.

Sector concentration uses the identical formula, grouping by `sector`
instead of by symbol.

*Code:* `concentration_engine.py`, `sector_risk_engine.py`

---

## 2. Correlation

**Pearson correlation** on aligned daily return series:
`corr(X, Y) = cov(X, Y) / (std(X) * std(Y))`, computed via
`numpy.corrcoef` on return histories aligned to the shortest common
length (most-recent-aligned, so older excess history from a longer-lived
symbol is dropped rather than padded with assumptions).

We report both **average pairwise correlation** and **maximum pairwise
correlation** because a portfolio can have low average correlation while
still containing one dangerously tight pair (e.g. two banks) — the
composite correlation score weights the maximum pair 60% and the average
40% for this reason.

**High-correlation pairs / clusters:** any pair >= `correlation_high_threshold`
(default 0.70) is flagged. Clusters of 3+ mutually high-correlated
symbols are found via a union-find over the high-correlation pair list
(`stop_loss_risk_engine._correlated_clusters`, reused by the stress-test
engine) — this is what lets the model say "these 4 positions are really
one bet" per the project's core design principle (spec section 3).

*Code:* `correlation_engine.py`

---

## 3. Covariance & portfolio volatility

Standard portfolio variance:

```
sigma_p = sqrt(w^T * Sigma * w)
```

`Sigma` is the sample covariance matrix of daily returns
(`numpy.cov`, `ddof=1`), annualized by `* sqrt(252)` for the volatility
figure reported in the output (daily figure retained internally for VaR).

**Numerical stability safeguards:**
- If the condition number of `Sigma` exceeds `1e8` (near-singular — e.g.
  two positions with identical or near-identical return histories), we
  apply **shrinkage toward the diagonal**: `Sigma' = (1-a)*Sigma + a*diag(Sigma)`,
  a simplified Ledoit-Wolf-style estimator. Configurable shrinkage
  (`covariance_shrinkage`, default 0.10; forced to >= 0.25 when
  near-singular).
- After shrinkage, eigenvalues are clipped to a small positive floor
  (`_nearest_psd`) to *guarantee* positive semi-definiteness before use —
  this is what prevents `sqrt()` of a negative variance from ever
  occurring, regardless of how pathological the input data is.
- If fewer than 2 positions have sufficient return history, we fall back
  to a **diagonal covariance built from each position's supplied
  `volatility` field** (zero assumed correlation). This is explicitly
  logged as `DIAGONAL_FALLBACK` in `data_quality` because it *understates*
  risk if positions are actually correlated — the model never claims
  more precision than the data supports.

*Code:* `covariance_engine.py`

---

## 4. Downside risk

Distinguished from symmetric volatility per spec section 5.E.

**Downside deviation** (semi-deviation below a minimum acceptable return,
MAR = 0 by default):

```
downside_deviation = sqrt( mean( (r_t - MAR)^2 for r_t < MAR ) )
```

computed on the **portfolio-level** historical return series (position
returns combined with capital weights), not position-by-position, so
diversification benefit is captured.

**Expected loss** = mean of the negative-return observations.
**Max observed adverse move** = single worst historical daily portfolio
return.

*Code:* `downside_risk_engine.py`

---

## 5. Value at Risk (VaR)

**Historical VaR** (no distributional assumption): the empirical
`(1-confidence)`-percentile of the historical portfolio return series,
reported as a positive loss fraction:

```
VaR_historical(c) = -percentile(portfolio_returns, (1-c)*100)
```

**Parametric VaR** (normal-distribution assumption), provided as a
secondary, always-computable cross-check:

```
VaR_parametric(c) = z(c) * sigma_p_daily
```

where `z(c)` is the inverse standard-normal CDF at confidence `c`
(`scipy.stats.norm.ppf`). Both are surfaced in the output — parametric
VaR is never presented as more accurate; it exists mainly for the case
where return history is too short for a reliable empirical quantile.

Both are scaled by `sqrt(horizon_days)` for the configured horizon
(default 1 day), following the standard square-root-of-time
approximation.

**This is explicitly not a guarantee** — VaR describes a historical/
distributional estimate of loss magnitude at a given confidence, not a
worst-case bound. This caveat is documented here and is not repeated as
a disclaimer inside every function; the number itself is just a
probabilistic estimate.

*Code:* `var_engine.py`

---

## 6. Expected Shortfall (CVaR)

```
ES(c) = E[ L | L > VaR(c) ]
```

Implemented as the mean of all historical portfolio returns at or below
the VaR threshold, negated to a positive loss fraction. If fewer than 5
observations fall in that tail, the estimate is flagged `LIMITED_DATA` —
an ES computed from 1-2 tail points is not a reliable tail estimate and
the model says so rather than reporting a precise-looking number.

*Code:* `expected_shortfall_engine.py`

---

## 7. Drawdown

```
current_drawdown = (peak_portfolio_value - current_portfolio_value) / peak_portfolio_value
```

reusing whatever `peak_portfolio_value` / `current_portfolio_value` the
Portfolio Manager already tracks (per spec section 32 — no duplicate
equity-curve computation). If a historical equity series is additionally
supplied, `max_drawdown`, `rolling_drawdown` (max drawdown over the most
recent `rolling_window` observations), and `drawdown_acceleration`
(change in mean drawdown between the recent half-window and the prior
half-window) are also computed.

Drawdown is bucketed into `NORMAL` / `ELEVATED` / `HIGH` / `CRITICAL`
bands (configurable thresholds), and the 0-100 score is a
piecewise-linear interpolation within the matched band.

*Code:* `drawdown_engine.py`

---

## 8. Capital utilization

```
invested_pct = invested_capital / portfolio_equity
cash_pct     = available_cash / portfolio_equity
```

Scored against `warn_invested_capital_pct` / `max_invested_capital_pct`,
with an **additional penalty** if `cash_pct` falls below
`min_cash_buffer_pct` even when `invested_pct` alone looks acceptable —
this is what stops the model from approving a portfolio that is "90%
invested, 10% cash" if the policy actually requires a bigger buffer for
the current regime.

*Code:* `capital_utilization_engine.py`

---

## 9. Liquidity

```
liquidity_ratio_i = market_value_i / avg_traded_value_i
```

Flags positions whose size is large relative to their own typical daily
traded value (a FILTER2.0-supplied metric, not recomputed here) — i.e.
positions that would be hard to exit without moving the price.

*Code:* `liquidity_risk_engine.py`

---

## 10. Stop-loss / worst-case risk

```
Risk_i = Quantity_i * |Entry_i - Stop_i|
PortfolioStopRisk = sum(Risk_i)
```

reported both in currency and as a percentage of total capital.
**Correlated stop risk** sums `Risk_i` only over symbols that fall inside
a high-correlation cluster (same union-find clustering as the
correlation engine) — this is the "how much could I lose at once if
these move together and all hit stops" figure, distinct from the flat
aggregate sum. Positions with no stop-loss price set are excluded from
the aggregate (their loss is unbounded) and this is flagged rather than
treated as zero risk; a fixed score penalty is applied when this occurs.

*Code:* `stop_loss_risk_engine.py`

---

## 11. Stress testing

Each configured `StressScenario` (fully user-editable in `config.py`,
per spec section 5.L) computes a portfolio loss estimate as a weighted
sum over positions:

```
loss_i = weight_i * ( market_shock * beta_i
                       + sector_shock * [position in target sector]
                       + correlated_cluster_shock * [position in a high-corr cluster]
                       + volatility_multiplier_adjustment
                       + liquidity_discount * [position is illiquid] )

portfolio_loss = sum(loss_i)
```

`beta_i` defaults to 1.0 when not supplied (flagged in `data_quality`).
The default scenarios (market -2%/-5%, sector -5%/-10%, correlated
cluster -7%, volatility shock, liquidity deterioration) are examples
from the spec, not hardcoded requirements — add, remove, or reweight
scenarios via `RiskConfig.stress_scenarios` without touching this file.

*Code:* `stress_test_engine.py`

---

## 12. Marginal / component risk contribution

Standard Euler decomposition of portfolio variance (this is the same
identity that makes component contributions sum exactly to portfolio
risk, which is why it's the standard tool for "which position is
secretly your riskiest one"):

```
MCTR_i   = (Sigma @ w)_i / sigma_p           # marginal contribution to risk
CCTR_i   = w_i * MCTR_i                       # component contribution to risk
%CCTR_i  = CCTR_i / sigma_p                   # as a fraction; sum over i = 1.0
```

A position is flagged "disproportionate" when
`%CCTR_i / w_i >= disproportion_threshold` (default 1.5x) — i.e. its
share of portfolio risk is at least 50% larger than its share of
capital.

*Code:* `marginal_risk_engine.py`

---

## 13. Composite portfolio_risk_score (0-100)

```
score = sum( weight_c * sub_score_c  for each category c )
```

using the documented weights in `config.DEFAULT_SCORE_WEIGHTS` (comments
in that file explain the rationale for each weight — concentration,
correlation, and tail risk are weighted highest at 0.15 each because
they are the three failure modes most likely to destroy a small
account; count-based and overlapping signals like capital utilization
and stop-loss are weighted lowest at 0.05 to avoid double-counting risk
that's already captured elsewhere). This is a **documented weighted
sum**, not a blind average, per spec section 7's explicit instruction.

Each `sub_score_c` (0-100) is itself a piecewise-linear ramp: 0 at half
the "warning" threshold, 25 at the warning threshold, 75 at the hard
limit, 100 at 2x the hard limit (implemented independently in each
engine so category-specific nonlinearities — e.g. drawdown's discrete
NORMAL/ELEVATED/HIGH/CRITICAL bands — can differ where appropriate).

*Code:* `scoring_engine.py`

---

## 14. Risk classification & capital-tier context

The composite score is bucketed into `LOW < 25 <= MODERATE < 45 <= HIGH
< 65 <= SEVERE < 85 <= CRITICAL` (configurable). Separately, capital is
bucketed into tiers (`MICRO` < ₹3,000, `VERY_SMALL` < ₹10,000, `SMALL` <
₹50,000, `MODERATE` < ₹2,00,000, `STANDARD` above). At `MICRO`/
`VERY_SMALL` tiers with fewer than 5 positions, a **contextual note** is
attached when concentration/sector/correlation scores are elevated,
explaining that full diversification may be economically infeasible
after transaction costs — but the numeric score and classification are
**never reduced** because of this; the note contextualizes risk, it does
not hide it.

*Code:* `scoring_engine.py`, `config.capital_tier()`

---

## 15. Risk limits (PASS / WARNING / BREACH)

Independent of the composite score. Each configured limit
(`RiskLimits` in `config.py`) is checked directly against its own
warn/max thresholds — this is what allows the model to say
"REJECT_NEW_POSITION" even when the composite score alone looks
moderate, because a single hard breach (e.g. one position at 35% of
the portfolio) is disqualifying on its own regardless of how the other
nine categories score.

*Code:* `risk_limit_engine.py`

---

## 16. PORTFOLIO_RISK_STATUS decision logic

```
if any critical-category breach (VaR, ES, stress loss, or CRITICAL drawdown)
   or composite risk_class == CRITICAL:
       EMERGENCY_REDUCTION
elif any limit breach:
       REDUCE_EXPOSURE   if 3+ breaches or risk_class == SEVERE
       REJECT_NEW_POSITION  otherwise
elif any limit warning:
       ACCEPT_WITH_WARNING
else:
       ACCEPT
```

This function receives **only realized risk metrics and limit
breaches** — it never looks at expected return or opportunity score. Per
spec section 29, risk always wins over return when they conflict; that
guarantee is structural here (the recommendation engine has no access to
return data at all), not a runtime check.

*Code:* `recommendation_engine.py`

---

## 17. Regime-aware thresholds

`RiskConfig.limits_for_regime(regime)` scales percentage-based limits
(concentration, volatility, VaR, ES, invested-capital, drawdown, stress
loss) by a configurable multiplier per regime (e.g. `STRONG_BEARISH`:
0.65x — tighter; `STRONG_BULLISH`: 1.15x — looser). Count-based limits
(position count) and structural limits (correlation, liquidity) are
**not** regime-scaled, since a bull market doesn't make two positions
less correlated or a stock more liquid. `UNKNOWN` regime defaults to a
conservative 0.90x multiplier rather than 1.0x, so an unrecognized
regime string errs toward caution rather than neutrality.

*Code:* `config.RiskConfig.limits_for_regime`

---

## 18. Risk budgeting

The same category weights used for the composite score double as a
100-point risk budget allocation (`budget_c = weight_c * 100`). Each
category "consumes" `budget_c * (sub_score_c / 100)` of its own
allocation; `category_remaining` and `total_remaining` tell Position
Sizing how much room is left before proposing a new trade.

*Code:* `risk_budget_engine.py`
