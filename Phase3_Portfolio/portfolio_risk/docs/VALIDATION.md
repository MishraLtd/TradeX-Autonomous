# Validation Report

## Test suite results

`python run_tests.py` (dependency-free runner) / `python -m pytest portfolio_risk/tests/ -v`

```
PASS  test_deterministic_output
PASS  test_empty_portfolio
PASS  test_highly_concentrated_portfolio
PASS  test_highly_correlated_portfolio
PASS  test_insufficient_historical_data
PASS  test_invalid_input_values
PASS  test_large_aggregate_stop_loss_exposure
PASS  test_large_number_of_positions
PASS  test_missing_price_data
PASS  test_proposed_trade_causes_breach
PASS  test_proposed_trade_improves_diversification
PASS  test_severe_drawdown
PASS  test_single_position_portfolio
PASS  test_single_sector_portfolio
PASS  test_singular_covariance_matrix
PASS  test_very_small_capital
PASS  test_well_diversified_portfolio

17 passed, 0 failed
```

All 16 scenarios required by spec section 22 pass, plus a determinism
check (identical input -> identical `portfolio_risk_score` across two
calls — confirms no hidden randomness or mutable global state).

## What each test demonstrates

| Test | Confirms |
|---|---|
| Empty portfolio | Returns a valid minimal-risk schema instead of crashing on division-by-zero (`portfolio_equity == 0`). |
| Single position | Concentration correctly identifies 100% of *invested* capital in one name; sector/correlation degrade gracefully with N=1. |
| Well-diversified (6 sectors) | Low concentration score, `PASS` on position-concentration limit, overall `LOW`/`MODERATE` class. |
| Highly concentrated (2 positions, 99:1) | Concentration `BREACH`, status escalates to `REJECT_NEW_POSITION` or worse. |
| Highly correlated (4 assets, ~0.9 pairwise) | Average correlation correctly estimated >0.6 from synthetic factor-model returns; correlation limit `WARNING`/`BREACH`. |
| Single-sector (5 positions, 1 sector) | Sector concentration = 100% of invested capital; `BREACH`. |
| Insufficient history (2 return observations) | Falls back safely, reports `data_quality != VALID`, still returns a finite score. |
| Missing price data (`None`/`0` price, `NaN` handling) | Invalid positions are excluded via `validation.py`, valid ones still assessed correctly. |
| Very small capital (₹1,000) | Correctly tagged `capital_tier: MICRO`; concentration still scored at full severity, not silently discounted. |
| Large number of positions (20) | `position_count` limit `BREACH` as expected. |
| Large aggregate stop-loss exposure (30% stop distance x3 positions) | `aggregate_stop_loss_risk` limit `BREACH`. |
| Proposed trade causes breach | `evaluate_new_trade` correctly identifies new breaches introduced by an oversized proposed position (also caught and fixed a real bug — see below). |
| Proposed trade improves diversification | Adding a small, uncorrelated, different-sector position does not increase sector concentration. |
| Severe drawdown (peak ₹12,000 -> current ₹9,000 = 25% dd) | `drawdown.state` reaches `HIGH`/`CRITICAL`; status escalates appropriately. |
| Singular covariance (two positions with identical return histories) | Shrinkage + eigenvalue clipping prevents `NaN`/crash; a finite, non-negative volatility is returned. |
| Invalid inputs (`NaN` capital, negative quantity, `NaN` quantity) | All invalid rows excluded; portfolio-level `NaN` capital replaced with 0.0 and flagged; no exception raised anywhere in the pipeline. |

## Manual end-to-end validation (`examples/example_portfolio.py`)

A 4-position, ₹100,000-capital portfolio (TCS, INFY, HDFCBANK,
SUNPHARMA — realistic sector mix, 52% invested) was assessed and
produced:

- `portfolio_risk_score: 28.01`, `risk_class: MODERATE`,
  `risk_status: ACCEPT_WITH_WARNING`
- Two correctly-identified drivers: position concentration (17.4%,
  above the 15% warning threshold) and sector concentration (28.7% IT,
  above the 25% warning threshold)
- All tail-risk, drawdown, and liquidity limits `PASS`, consistent with
  the conservative, well-spread inputs used

A subsequent new-trade pre-check proposed adding a large (40-share)
WIPRO position in the same IT sector:

- Score moved `28.01 -> 38.09` (+10.08)
- Correctly flagged two **new** breaches: `position_concentration`
  (would reach 31.6%) and `sector_concentration` (would reach 51.2%)
- Final recommendation: `REJECT_NEW_POSITION`, with the specific
  breached limits and percentages named in
  `decision_explanation` — matching the exact output format specified
  in spec section 10's worked example.

This confirms the core "what happens if I add this trade?" workflow
(spec section 11) behaves correctly end-to-end, not just at the
unit-test level.

## A real bug caught during validation

While reviewing the example output, the `marginal_risk_engine`'s
"disproportionate risk contribution" flag was firing for **all four**
positions simultaneously — including ones with unremarkable risk
profiles. Root cause: `capital_weight` was expressed as a fraction of
**total portfolio equity** (cash + invested), while
`risk_contribution_pct` sums to 1.0 across **invested positions only**
(idle cash carries zero risk by construction). Any time a portfolio
holds meaningful cash (this example was 48% cash), every invested
position's risk share looks inflated relative to its total-equity
weight — a systematic false positive, not a meaningful signal.

**Fix:** the disproportion check now compares risk contribution against
each position's weight *as a fraction of invested capital*, which is
the correct like-for-like comparison. `capital_weight` in the output
schema is unchanged (still expressed relative to total equity, since
that's the useful figure for comparison against `concentration.weights`
elsewhere in the report) — only the internal disproportion test was
corrected. Verified via `marginal_risk_engine.py` and re-run of the full
test suite (still 17/17) plus the example script (no longer flags all
four positions).

This is disclosed here deliberately: the validation process is only
useful if it surfaces real defects, not just confirms the code runs.

## Known unvalidated areas

- No live NSE market data was used anywhere in this validation — all
  return histories are synthetic (`numpy` normal draws, optionally with
  a shared factor for the correlation test). Behavior on real,
  fat-tailed, autocorrelated equity return data has not been checked
  and should be a priority before production use — see
  `docs/LIMITATIONS.md`.
- Backtest-style validation (predicted risk vs. realized outcomes,
  spec section 23) has not been performed; the architecture supports it
  (pure functions of `PortfolioState`, no look-ahead), but no historical
  backtest has actually been run yet.
