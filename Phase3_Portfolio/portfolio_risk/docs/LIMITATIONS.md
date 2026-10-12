# Known Limitations & Assumptions

Explicit, so nobody mistakes this for more complete than it is (spec
sections 30 and 33-H require this).

## Statistical / modeling limitations

1. **Historical-data dependent.** Correlation, covariance, VaR,
   Expected Shortfall, and downside deviation all require sufficient
   `return_history` on each position. Below `min_history_days` (default
   30), these degrade to fallbacks (diagonal covariance from supplied
   `volatility`, parametric-only VaR, or `UNAVAILABLE`) that are less
   informative and are explicitly flagged in `data_quality` — but a
   flag is not a fix. A newly-listed stock or a strategy that trades
   symbols with thin history will regularly hit these fallbacks.

2. **Normal-distribution assumption in parametric VaR only.** Historical
   VaR makes no distributional assumption, but is only as good as the
   historical sample — it will understate tail risk if the available
   history didn't include a genuine tail event (a common criticism of
   historical VaR generally, not specific to this implementation).

3. **No fat-tail / jump modeling.** Indian equities can gap significantly
   on news, corporate actions, or circuit-filter days. Nothing here
   models jump risk explicitly; the stress-test engine's configurable
   scenarios are the intended way to compensate (see #10 below), but
   they are still linear shock approximations, not a jump-diffusion
   model.

4. **Correlation estimated from historical returns only.** Correlation
   is not forward-looking; it can break down exactly when it matters
   most (correlations often spike toward 1.0 in genuine market
   stress, which is *conceptually* why the `correlated_cluster_shock`
   stress scenario and `correlated_stop_risk` figure exist as a
   partial mitigation) — but the correlation *matrix itself* still
   reflects the recent past, not a stress regime.

5. **Beta defaults to 1.0 when missing.** Every stress scenario that
   uses `market_shock` will use `beta=1.0` for any position lacking a
   supplied beta, which is a real assumption, not a neutral default —
   it will overstate market-shock loss for genuinely low-beta names
   and understate it for high-beta ones. Always flagged in
   `data_quality` when it occurs.

## Design-scope limitations (deliberately out of scope for v1)

6. **Stock-level `predicted_risk` from the Risk Prediction Model is
   carried through the `Position` object but not blended into the
   portfolio score.** The portfolio-level model relies on `volatility`
   and `return_history` instead. If the upstream Risk Prediction Model
   produces a materially different risk estimate (e.g. from a more
   sophisticated model than realized volatility), that signal is
   currently not reconciled with this model's own volatility-based
   view. A future iteration could blend the two, but spec section 24
   explicitly forbids requiring ML for the baseline, and reconciling
   two independent risk estimates is a design decision, not a bug fix.

7. **Rebalance-cost-aware recommendations (spec section 15) are not
   implemented.** The model can tell you a position is too concentrated;
   it does not currently calculate "the transaction cost to fix this
   exceeds the risk benefit." `expected_net_pnl` and `transaction_cost`
   are carried on `Position` for exactly this purpose but no engine
   consumes them yet. This is the most significant spec item **not**
   delivered in this iteration — see "Next recommended integration
   step" in the final summary.

8. **No `MAX_ACCEPTABLE_POSITION_SIZE` as a direct output field.**
   `docs/INTEGRATION.md` shows how Position Sizing can derive it from
   `risk_limits` thresholds and `portfolio_equity`, but it is not
   returned as a first-class field in the schema. Straightforward to
   add; not done here because it's a thin derived value and adding it
   without a concrete Position Sizing consumer risked guessing at a
   contract shape.

9. **`daily_pnl` / `cumulative_pnl` fields exist on `PortfolioState` but
   are not consumed by any engine.** They're carried through for
   completeness/logging (the schema in spec section 4 lists them) but
   no current risk calculation uses them. Drawdown uses
   `peak_portfolio_value` / `current_portfolio_value` instead.

10. **Stress scenarios are linear and configurable, not calibrated.**
    The default scenarios (`config.DEFAULT_STRESS_SCENARIOS`) are
    reasonable illustrative examples (-2%/-5% market, -5%/-10% sector,
    etc.) as invited by spec section 5.L, but they are not derived
    from any historical Indian-market stress episode. Before relying on
    stress-test output for real capital decisions, calibrate the
    scenario magnitudes against actual historical NSE drawdown episodes
    for the relevant sectors/market-cap segments.

11. **No live backtest has been run.** The architecture is designed to
    support historical validation without look-ahead bias (every
    function is a pure transform of a `PortfolioState` snapshot; no
    engine reaches outside the data it's given), but spec section 23's
    "predicted risk vs. realized risk" comparison has not actually been
    executed against real historical TradeX portfolio snapshots.

12. **`pandas` is listed as a dependency but not currently used directly
    by any engine.** `numpy` and `scipy` (for the inverse-normal CDF)
    are the only libraries actually imported. `pandas` was reserved in
    case future backtest/reporting tooling needs it; remove it from
    the dependency list if a stricter dependency audit is required.

13. **Duplicate-position merging in `validation.py` uses a simple
    weighted-average entry price.** This is a reasonable default for
    "the same symbol appears twice in the position list due to an
    upstream data glitch," but it is not a substitute for correct
    position-aggregation logic in Portfolio Manager itself — this
    model should not be relied upon as the source of truth for
    resolving duplicate broker-side positions.

14. **Regime multipliers (`config.RiskConfig.regime_multipliers`) are
    illustrative defaults**, not calibrated against historical regime
    transitions. The mechanism (scaling percentage-based limits by a
    per-regime multiplier) is real and tested; the specific multiplier
    values (e.g. `STRONG_BEARISH: 0.65`) are reasonable starting points
    that should be reviewed against how the Market Regime Model's
    labels actually perform historically.

## Numerical caveats

15. Portfolio volatility, VaR, and ES are all computed in **daily**
    terms internally and annualized only for the volatility figure
    (`* sqrt(252)`); VaR/ES use `sqrt(horizon_days)` scaling for the
    configured horizon (default 1 day). Comparing an annualized
    volatility limit against a 1-day VaR limit requires understanding
    which scaling was applied to which number — see
    `docs/METHODOLOGY.md` sections 3 and 5.

16. The `sqrt(time)` scaling used for both annualization and VaR
    horizon-scaling assumes i.i.d. returns, which real markets violate
    (volatility clustering, autocorrelation). This is a standard,
    widely-used approximation, not a claim of precision.
