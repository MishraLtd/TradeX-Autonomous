# TradeX — Phase 2: Return Prediction Engine

Status: **EXPERIMENTAL**. This is a working, tested skeleton — not a
production model. It has never seen real NSE data. Every number it
would report on real data (accuracy, calibration, Sharpe) is currently
unknown; what's proven is that the *plumbing* is leakage-safe and the
*rejection paths* fire correctly, verified against synthetic data.

## 1. What this is / isn't

This predicts a **return distribution**, not a buy/sell signal
(Section 39 — the model never places trades). It sits between
**FILTER2.0** (universe filtering, unchanged) and your **Phase 1 Cost
Model** (unchanged, integrated via `integration/cost_model_bridge.py`).

```
FILTER2.0 → Feature Engine → Return Model → Cost Model → Opportunity Engine
```

## 2. Package layout

```
return_model/
  config.py            horizons, thresholds, versioning, engine config
  schemas.py            RawPrediction / NetPrediction / registry output contract (pydantic)
  targets/definitions.py   7 target variants incl. triple-barrier, MFE/MAE — timing documented
  features/engine.py    price/trend/vol/volume/momentum/market/time features (leakage-guarded)
  datasets/builder.py   assembles (features, targets) with universe-membership + leakage boundary
  walk_forward/splitter.py  purged + embargoed expanding-window CV
  preprocessing/        normalization (rolling + train-only) and data/feature quality checks
  models/                baselines (Sec 9) + tree/boosting candidates (RF/GBM/XGBoost*/LightGBM*)
  uncertainty/quantile.py  QuantileGBM + split-conformal prediction intervals
  calibration/calibrator.py  isotonic/Platt calibration + reliability diagram + Brier/ECE
  ranking/cross_sectional.py  Top-K ranking and backtest summary
  registry/model_registry.py  SQLite-backed model registry + prediction/eval tables
  inference/            OOD detection + live inference engine (output-contract assembly)
  explainability/explain.py  SHAP (if installed) / permutation importance / perturbation fallback
  evaluation/metrics.py  ML metrics, trading metrics, cost-aware comparison, bootstrap CI
  integration/cost_model_bridge.py  Protocol-based interface to your real Phase 1 Cost Model
  integration/end_to_end_example.py  runnable demo, FILTER2.0-survivor → net prediction
  tests/test_core.py     pytest suite — leakage guards, purge/embargo integrity, rejection paths
```

*XGBoost/LightGBM are optional; the code falls back to sklearn's
GradientBoostingRegressor if they aren't installed (Section 36).

## 3. Target definition (Section 3)

Implemented and compared: close-to-close, open-to-close,
entry-to-exit (configurable lag + exit price column), forward MFE/MAE,
and triple-barrier labeling. **Default primary training target is
`entry_to_exit_return`** with a 1-bar entry lag (next bar's open) —
close-to-close is kept only as a sanity-check baseline because it
assumes an unrealistic fill price. Every target function's timing
convention (decision time vs. entry time vs. exit time) is documented
in the module docstring — read it before changing anything.

## 4. Leakage controls (Section 4) — how they're actually enforced

1. **Features**: every rolling/EWM computation in `features/engine.py`
   only reads rows `<= t` for row `t`. Enforced by a unit test that
   greps the source for `shift(-` (a negative shift is the only way to
   pull future data into a feature) — see
   `test_features_have_no_negative_shift`.
2. **Targets**: intentionally look forward (that's what makes them
   labels) — but are computed in a *separate* module and only joined to
   features at `datasets/builder.py`, which is the single place the
   leakage boundary is crossed on purpose.
3. **Splits**: `walk_forward/splitter.py` implements purge (drop
   training rows whose label window overlaps validation) + embargo
   (buffer after validation before test starts), sized from
   `LABEL_OVERLAP` + `EMBARGO_EXTRA_BARS` in `config.py`. Verified by
   `test_walk_forward_purge_creates_a_gap_before_validation`.
4. **Normalization**: `TrainOnlyNormalizer` fits once on the training
   slice only; `RollingNormalizer` uses `shift(1)` before any rolling
   stat.
5. **Survivorship bias**: `datasets/builder.py` accepts an optional
   `SymbolUniverseHistory` (historical FILTER2.0 eligibility windows) and
   filters rows to it — so a backtest doesn't accidentally use today's
   universe to simulate 2022 decisions. **This currently needs to be
   wired to your actual FILTER2.0 historical output; right now nothing
   enforces it if you don't pass it in.**

## 5. What's genuinely implemented vs. stubbed

**Implemented and tested:**
- Leakage-safe feature engine (7 families, ~40 features)
- 7 target definitions incl. triple-barrier
- Purged/embargoed walk-forward splitter
- Baselines (zero/mean/median/momentum/linear) + tree/boosting candidates
- Quantile regression (QuantileGBM) + split-conformal intervals with a
  real finite-sample coverage guarantee
- Isotonic/Platt probability calibration + ECE/Brier/reliability diagram
- OOD detection (IsolationForest) feeding into LOW_TRUST status
- Cross-sectional ranking + Top-K backtest summary
- SQLite model registry with promotion rules (won't silently overwrite
  PRODUCTION) and full prediction/evaluation table schema
- Cost Model integration point (`CostModelProtocol`) — **you must wire
  your actual Phase 1 Cost Model object into this; a `NullCostModel`
  placeholder is provided only so the pipeline runs end-to-end today**
- Output contract exactly matching Section 38's schema
- End-to-end runnable example, 10-test pytest suite, all passing

**Explicitly stubbed / needs your input before PRODUCTION:**
- Real NSE OHLCV ingestion — `_load_symbol_ohlcv` in the example is
  synthetic. Wire it to your Supabase-backed NSE pipeline.
- Real FILTER2.0 historical membership — `SymbolUniverseHistory` is a
  plain dataclass; you need to populate it from FILTER2.0's actual
  historical output, not just its current universe.
- Threshold values (`CANDIDATE_RETURN_THRESHOLDS_PCT`,
  triple-barrier TP/SL levels, OOD contamination rate, confidence
  scale) are **starting defaults, not derived values** — Section 11/20
  requires deriving them from real volatility/cost data.
- Microstructure features (bid/ask, depth) — left undone; Kite
  historical depth data isn't reliably available for backtesting.
- Regime-specific models (Section 14) — only the "one global model
  with regime features" path exists; the regime-specific and hybrid
  variants aren't built. Needs real regime-labeled data to compare.
- Drift monitoring / retraining triggers (Sections 44-45) — the
  registry schema supports storing metrics over time, but the
  monitoring job itself (compute drift, decide to retrain) isn't built.
- SHAP is optional (falls back to a weaker perturbation method if not
  installed) — `pip install shap` for production explainability quality.

## 6. Final self-critique (Section 50)

Honest answers, not reassurances:

1. **Is the target actually tradable?** `entry_to_exit_return` uses
   next-bar-open entry, which is realistic for delivery. For intraday
   it still assumes you can execute at the next bar's open exactly —
   real slippage on a ₹1,000 account with thin liquidity could differ
   materially. Not yet validated against real fills.
2. **Could any feature contain future information?** Guarded by
   construction + a static test, but the static test only catches
   literal `shift(-N)` calls — it would NOT catch, e.g., a feature
   library function that internally centers a rolling window. Every
   new feature needs manual review, not just the grep test.
3. **Survivorship bias?** Structurally prevented if you pass real
   `SymbolUniverseHistory`; **currently unenforced** if you don't,
   since nothing stops you from calling `build_symbol_panel` without it.
4. **Overlapping labels?** Purge/embargo widths (`LABEL_OVERLAP`,
   `EMBARGO_EXTRA_BARS`) are conservative *guesses* at this stage, not
   empirically validated against your actual bar frequency.
5. **Is validation truly chronological?** Yes, by construction of
   `expanding_walk_forward_folds` — verified by test.
6. **Is probability calibrated?** The calibration machinery works
   (demonstrated reducing ECE on synthetic data) but has never been
   validated on real, non-synthetic label distributions.
7. **Survives realistic costs?** `evaluation/metrics.py` has the
   zero-cost-vs-realistic-cost comparison function, but it's never been
   run against real Cost Model output — only `NullCostModel`.
8. **Beats simple baselines?** On synthetic (near-random-walk) data,
   the GBM barely beats momentum and both barely beat historical mean —
   expected, since synthetic returns have almost no real structure.
   This tells you nothing about real-market performance.
9. **Survives multiple regimes?** Not tested — no regime-labeled real
   data exists yet in this codebase.
10. **Survives unseen time periods?** Only one fold has been run in the
    demo; production use requires running and comparing ALL
    walk-forward folds, not cherry-picking the best one.
11. **Useful at ₹1,000?** Capital-aware analysis (Section 43) hooks
    exist in config (`CAPITAL_LEVELS_INR`) but no analysis has actually
    been run — that requires the real Cost Model's friction curve.
12. **Does uncertainty affect confidence properly?** Yes mechanically —
    the end-to-end demo shows predictions correctly downgraded to
    LOW_TRUST when conformal intervals are wide relative to the
    predicted magnitude. Whether the specific `scale` parameter is
    well-tuned for real NSE volatility is unknown.
13. **Learning regime vs. stock-specific edge?** Can't be answered yet —
    needs real market-context features tested against real data with
    regime labels.
14. **Exploiting illiquid, unexecutable stocks?** FILTER2.0 is assumed
    to already gate on liquidity upstream; this engine trusts that
    gate and does not independently re-check executability.
15. **Disappears after slippage?** Unknown — Cost Model not yet wired
    in for real.
16. **Statistically significant edge?** `bootstrap_confidence_interval`
    is implemented but has only been run conceptually, never against
    real backtest output.
17. **Stable enough for production?** No — this is EXPERIMENTAL by
    design. The registry enforces that a model must be explicitly
    promoted through VALIDATED → PAPER → PRODUCTION; nothing here
    should go live without that process and real data behind it.

## 7. Immediate next steps to make this real

1. Wire `_load_symbol_ohlcv` (or the builder directly) to your actual
   Supabase NSE data.
2. Populate `SymbolUniverseHistory` from FILTER2.0's historical output.
3. Implement a `CostModelProtocol` adapter around your real Phase 1
   Cost Model class and swap out `NullCostModel`.
4. Run `expanding_walk_forward_folds` across ALL folds (not just the
   last one) on real data, log train/val/test metrics per fold via the
   registry, and only then decide on threshold values, TP/SL levels,
   and the confidence `scale` parameter empirically.
5. Re-run the Section 50 self-critique against real results before any
   promotion past EXPERIMENTAL.
