# TradeX Risk Prediction Model — Phase 1 (Baseline Layer)

This is the **first implementation slice** of the Risk Model specified in
`ARCHITECTURE.md`. Per the project's own requirement (never jump straight
to ML — build naive baseline, then statistical baseline, then ML only if
it wins out-of-sample), this phase ships:

- Causal feature engineering (`features.py`) with an automated leakage
  test (`tests/test_leakage.py`) that mutates future bars and asserts past
  feature values don't move.
- Causal label construction for MAE/MFE, stop-vs-target race, and
  overnight gap (`labels.py`), with explicit `entry_timestamp` /
  `target_window_end_timestamp` fields so leakage is *checkable*, not just
  assumed.
- A purged, embargoed, chronological walk-forward splitter
  (`validation.py`) — never random k-fold on trade rows.
- Three baselines in increasing sophistication (`baselines.py`):
  1. Naive unconditional frequency
  2. Volatility-based Monte Carlo first-passage simulation (needs **zero**
     historical trade samples — this is the Phase-1 default inference
     path in `inference.py`)
  3. Calibrated logistic regression (Platt scaling) + quantile
     gradient-boosting for the MAE distribution
- Calibration diagnostics (`calibration.py`): Brier score, log loss,
  expected calibration error, reliability curves.
- A simple, transparent Mahalanobis-distance OOD detector (`ood.py`).
- A documented, non-black-box risk score formula (`scoring.py`).
- A strongly-typed Pydantic output schema (`schema.py`) matching Section
  20 of the spec, with explicit model/feature/config versioning.
- `inference.py::predict_risk()` — the end-to-end entry point.

## Running the tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

12/12 tests pass as of this delivery, including the look-ahead-bias test,
the purge/embargo overlap test, and an end-to-end inference smoke test.

## What's deliberately NOT in this phase

- No fitted ML model is wired into `predict_risk()` yet — Phase 1 uses the
  Monte Carlo baseline so the system can run before any historical
  trade-outcome dataset exists. Wiring in Baseline 3 (calibrated logistic)
  and the quantile-MAE model requires real OHLCV history from the
  project's Supabase `ohlcv_daily` table plus a fixed trade-configuration
  convention (stop/target distances) from the Return Model / strategy —
  see "Phase 2" in ARCHITECTURE.md.
- No portfolio-level / correlation risk (Section 27-28) — this is
  single-trade risk only, as an input to a future Portfolio Risk Engine.
- No gap-risk model beyond a placeholder constant — needs real overnight
  gap history per symbol to fit (Section 10).
- No drift monitoring / realized-vs-predicted feedback loop (Sections
  43-44) — needs a live trade journal to compare against.
- No database persistence layer — schema is specified in ARCHITECTURE.md
  but not yet implemented against Supabase/Postgres.

## Extending to Phase 2

1. Pull real OHLCV from `ohlcv_daily` (already the project's data source
   per the Step 10A/10B ingestion pipeline) instead of the synthetic
   fixture in `tests/conftest.py`.
2. Fix a trade-configuration convention (how stop/target distances are
   set — presumably from the Return Model / strategy layer) and run
   `labels.build_label_frame` per symbol to build a real training set.
3. Fit `baselines.build_calibrated_logistic()` and
   `baselines.build_quantile_mae_model()` per trade_type (INTRADAY vs
   DELIVERY, per Section 15 — do not pool them).
4. Compare Baseline 3 against Baseline 2 out-of-sample using
   `PurgedWalkForwardSplit` + `calibration_report` + the trading-utility
   metrics in Section 35/47 (A/B: with vs without risk model). Only
   promote Baseline 3 into `inference.py` if it wins.
5. Fit `OODDetector` on the real training feature distribution and wire
   it into `predict_risk()`.
