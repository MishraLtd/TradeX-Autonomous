"""
End-to-end integration example — Section 47.

FILTER2.0 survivor -> Feature Engine -> Return Prediction -> Cost Model
-> Net Expected Return

Runs entirely on synthetic data so it's reproducible without live NSE
data. Replace `_load_symbol_ohlcv` with your real FILTER2.0/Supabase
data source when wiring this in for real.

Run: python -m return_model.integration.end_to_end_example
"""

from __future__ import annotations
import numpy as np
import pandas as pd

from ..config import Horizon, EngineConfig
from ..features.engine import FeatureEngine
from ..datasets.builder import build_symbol_panel
from ..walk_forward.splitter import expanding_walk_forward_folds
from ..models.boosting import gradient_boosting_model
from ..models.baselines import HistoricalMeanBaseline, MomentumBaseline
from ..uncertainty.quantile import QuantileGBM, ConformalIntervals
from ..calibration.calibrator import ProbabilityCalibrator, expected_calibration_error
from ..inference.ood import OODDetector
from ..inference.engine import ModelBundle, predict_one
from ..integration.cost_model_bridge import NullCostModel, apply_cost_model
from ..evaluation.metrics import ml_regression_metrics, trading_metrics
from ..ranking.cross_sectional import rank_candidates_at_timestamp
from ..registry.model_registry import ModelRegistry
from ..schemas import ModelRegistryEntry, ModelStatus
from datetime import datetime, timezone


def _load_symbol_ohlcv(symbol: str, n=800, seed=0) -> pd.DataFrame:
    """STUB — replace with real FILTER2.0-eligible OHLCV pull (e.g. from
    Supabase, per Siddhant's existing NSE scanner pipeline)."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-03", periods=n, freq="B")
    drift = 0.0003 if symbol != "WEAK" else -0.0002
    ret = rng.normal(drift, 0.018, n)
    close = 100 * np.cumprod(1 + ret)
    open_ = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.005, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.005, n)))
    volume = rng.integers(50_000, 2_000_000, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def run_example():
    cfg = EngineConfig()
    horizon = Horizon.SWING_3D
    symbols = ["ALPHA", "BETA", "GAMMA", "WEAK"]
    fe = FeatureEngine(feature_version="v1")

    # 1. FILTER2.0 survivor -> Feature Engine -> targets
    panels = {}
    for i, sym in enumerate(symbols):
        ohlcv = _load_symbol_ohlcv(sym, seed=i)
        panels[sym] = build_symbol_panel(sym, ohlcv, horizon, fe)
    pooled = pd.concat(panels.values()).sort_index()
    print(f"[1] pooled panel: {pooled.shape[0]} rows x {pooled.shape[1]} cols across {len(symbols)} symbols")

    feature_cols = [c for c in pooled.columns if c not in
                    ("symbol", "horizon", "universe_eligible") and not c.startswith("target_")]

    # 2. Walk-forward split (purged/embargoed)
    folds = expanding_walk_forward_folds(pooled, horizon, n_folds=3)
    print(f"[2] {len(folds)} walk-forward folds generated")
    fold = folds[-1]  # use most recent fold for this demo
    train = pooled.loc[pooled.index.isin(fold.train_idx)]
    val = pooled.loc[pooled.index.isin(fold.val_idx)]
    test = pooled.loc[pooled.index.isin(fold.test_idx)]
    print(f"    train={len(train)} val={len(val)} test={len(test)} "
          f"(train ends {fold.train_range[1].date()}, val starts {fold.val_range[0].date()})")

    X_train, y_train = train[feature_cols], train["target_entry_to_exit"]
    X_val, y_val = val[feature_cols], val["target_entry_to_exit"]
    X_test, y_test = test[feature_cols], test["target_entry_to_exit"]

    # 3. Baselines vs candidate model (Sec 9)
    mean_bl = HistoricalMeanBaseline().fit(X_train, y_train)
    mom_bl = MomentumBaseline().fit(X_train, y_train)
    gbm = gradient_boosting_model().fit(X_train.fillna(0.0), y_train)

    print("[3] baseline vs candidate (test set MAE/directional accuracy):")
    for name, model in [("historical_mean", mean_bl), ("momentum", mom_bl), ("gbm_huber", gbm)]:
        pred = model.predict(X_test if name == "gbm_huber" else X_test)
        m = ml_regression_metrics(y_test, pred)
        print(f"    {name:16s} mae={m.get('mae', float('nan')):.4f} dir_acc={m.get('directional_accuracy', float('nan')):.3f}")

    # 4. Probability-positive classifier + calibration (Sec 11, 13)
    from sklearn.linear_model import LogisticRegression
    y_train_pos = (y_train > 0).astype(int)
    y_val_pos = (y_val > 0).astype(int)
    prob_clf = LogisticRegression(max_iter=500).fit(X_train.fillna(0.0), y_train_pos)
    raw_val_probs = prob_clf.predict_proba(X_val.fillna(0.0))[:, 1]
    ece_before = expected_calibration_error(raw_val_probs, y_val_pos)
    calibrator = ProbabilityCalibrator("isotonic").fit(raw_val_probs, y_val_pos)
    ece_after = expected_calibration_error(calibrator.transform(raw_val_probs), y_val_pos)
    print(f"[4] calibration: ECE before={ece_before:.4f} after={ece_after:.4f}")

    # 5. Quantiles + conformal interval (Sec 12, 28)
    qgbm = QuantileGBM(quantiles=cfg.quantiles).fit(X_train, y_train)
    conformal = ConformalIntervals(gradient_boosting_model()).fit(X_train, y_train)
    conformal.calibrate(X_val, y_val, alphas=(0.20,))

    # 6. OOD detector (Sec 30)
    ood = OODDetector().fit(X_train)

    # 7. Assemble model bundle + run inference for latest row per symbol
    bundle = ModelBundle(
        model_id="tradex_return_gbm", model_version="v0.1-experimental",
        data_version="feat=v1|tgt=v1|uni=synthetic|asof=2024-demo",
        point_model=gbm, prob_positive_model=_WithCalibration(prob_clf, calibrator),
        threshold_models={}, quantile_model=qgbm, conformal=conformal, ood_detector=ood,
    )

    cost_model = NullCostModel()
    net_predictions = []
    for sym in symbols:
        latest_row = panels[sym][feature_cols].iloc[[-1]]
        raw = predict_one(bundle, sym, horizon.value, latest_row, cfg, n_train_observations=len(X_train))
        net = apply_cost_model(raw, cost_model, capital_inr=1000)
        net_predictions.append(net)

    print("[7] net predictions (after Cost Model):")
    rows = []
    for net in net_predictions:
        r = net.raw
        print(f"    {r.symbol:6s} gross={r.expected_gross_return_pct:+.3f}% "
              f"net={net.expected_net_return_pct:+.3f}% conf={r.confidence:.2f} "
              f"status={r.status.value} viable={net.economically_viable}")
        rows.append({
            "symbol": r.symbol, "expected_net_return_pct": net.expected_net_return_pct,
            "probability_positive": r.probability_positive, "confidence": r.confidence,
        })

    # 8. Cross-sectional ranking (Sec 17)
    ranked = rank_candidates_at_timestamp(pd.DataFrame(rows))
    print("[8] ranked candidates:")
    print(ranked[["rank", "symbol", "expected_net_return_pct", "rank_score"]].to_string(index=False))

    # 9. Register model (Sec 32)
    reg = ModelRegistry(db_path="/home/claude/tradex_return_model_demo.db")
    reg.register(ModelRegistryEntry(
        model_id=bundle.model_id, model_version=bundle.model_version, horizon=horizon.value,
        training_start=fold.train_range[0].to_pydatetime(), training_end=fold.train_range[1].to_pydatetime(),
        feature_version="v1", target_definition="entry_to_exit_return",
        hyperparameters={"n_estimators": 300, "max_depth": 3, "loss": "huber"},
        training_metrics=ml_regression_metrics(y_train, gbm.predict(X_train.fillna(0.0))),
        validation_metrics=ml_regression_metrics(y_val, gbm.predict(X_val.fillna(0.0))),
        test_metrics=ml_regression_metrics(y_test, gbm.predict(X_test.fillna(0.0))),
        calibration_metrics={"ece": ece_after},
        data_version=bundle.data_version, status=ModelStatus.EXPERIMENTAL,
        created_at=datetime.now(timezone.utc),
    ))
    print("[9] model registered as EXPERIMENTAL in", reg.db_path)
    reg.close()


class _WithCalibration:
    """Wraps a raw classifier + fitted calibrator behind predict_proba."""
    def __init__(self, clf, calibrator):
        self.clf = clf
        self.calibrator = calibrator

    def predict_proba(self, X):
        raw = self.clf.predict_proba(X)[:, 1]
        calibrated = self.calibrator.transform(raw)
        return np.column_stack([1 - calibrated, calibrated])


if __name__ == "__main__":
    run_example()
