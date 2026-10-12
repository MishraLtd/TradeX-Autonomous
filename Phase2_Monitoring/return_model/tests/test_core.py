"""
Core test suite. Uses synthetic OHLCV data (no external data dependency)
so it runs anywhere. Focus is on the things that are cheap to get wrong
and expensive to have wrong: leakage in targets/features, walk-forward
purge/embargo integrity, and the output-contract rejection paths.

Run with: pytest -q return_model/tests/test_core.py
"""

from __future__ import annotations
import numpy as np
import pandas as pd
import pytest

from return_model.features.engine import FeatureEngine
from return_model.targets.definitions import (
    close_to_close_return, open_to_close_return, triple_barrier_label, build_target_panel,
)
from return_model.config import Horizon
from return_model.walk_forward.splitter import expanding_walk_forward_folds
from return_model.datasets.builder import build_symbol_panel, HORIZON_TO_BARS
from return_model.preprocessing.quality import validate_ohlcv, validate_feature_matrix
from return_model.calibration.calibrator import ProbabilityCalibrator, expected_calibration_error


def _synthetic_ohlcv(n=400, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    ret = rng.normal(0.0005, 0.015, n)
    close = 100 * np.cumprod(1 + ret)
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    volume = rng.integers(10_000, 500_000, n)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx)


def test_features_have_no_negative_shift():
    """Static guard: nothing in the feature engine source should use a
    negative shift (that would be look-ahead)."""
    import inspect
    from return_model.features import engine as feat_mod
    src = inspect.getsource(feat_mod)
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith(("#", '"""', "'''")) and "grep for" not in line
    )
    assert "shift(-" not in code_only, "found a negative shift in FeatureEngine — this is a leakage bug"


def test_target_requires_forward_data_and_is_nan_at_tail():
    df = _synthetic_ohlcv(100)
    tgt = close_to_close_return(df, horizon_bars=5)
    assert tgt.iloc[-5:].isna().all(), "last 5 rows must be NaN (insufficient forward data), never imputed"
    assert tgt.iloc[:-5].notna().all()


def test_open_to_close_uses_next_bar_open_not_same_bar_close():
    df = _synthetic_ohlcv(50)
    tgt = open_to_close_return(df, horizon_bars=1)
    expected_row0 = df["close"].iloc[1] / df["open"].iloc[1] - 1.0
    assert np.isclose(tgt.iloc[0], expected_row0)


def test_triple_barrier_label_values_are_valid():
    df = _synthetic_ohlcv(200)
    tb = triple_barrier_label(df, horizon_bars=5, take_profit_pct=0.02, stop_loss_pct=0.01)
    assert set(tb["label"].dropna().unique()).issubset({-1.0, 0.0, 1.0})
    assert tb["hit_type"].dropna().isin(["tp", "sl", "vertical", ""]).all()


def test_feature_engine_produces_finite_values_after_burn_in():
    df = _synthetic_ohlcv(300)
    fe = FeatureEngine()
    feats = fe.transform(df)
    # after the longest lookback (200-bar SMA), everything should be finite
    tail = feats.iloc[220:]
    assert np.isfinite(tail.select_dtypes(include=[np.number]).to_numpy()).mean() > 0.95


def test_walk_forward_folds_never_let_val_precede_train_cut():
    df = _synthetic_ohlcv(500)
    fe = FeatureEngine()
    panel = build_symbol_panel("TEST", df, Horizon.SWING_5D, fe)
    folds = expanding_walk_forward_folds(panel, Horizon.SWING_5D, n_folds=3)
    assert len(folds) > 0
    for f in folds:
        assert f.train_range[1] < f.val_range[0], "train must end strictly before validation starts"
        assert f.val_range[1] <= f.test_range[0], "validation must not overlap test"


def test_walk_forward_purge_creates_a_gap_before_validation():
    df = _synthetic_ohlcv(500)
    fe = FeatureEngine()
    panel = build_symbol_panel("TEST", df, Horizon.SWING_5D, fe)
    folds = expanding_walk_forward_folds(panel, Horizon.SWING_5D, n_folds=3)
    for f in folds:
        gap = f.val_range[0] - f.train_range[1]
        assert gap > pd.Timedelta(days=0), "purge gap between train and validation must be positive"


def test_data_quality_flags_bad_prices():
    df = _synthetic_ohlcv(60)
    df.iloc[10, df.columns.get_loc("close")] = -5.0
    report = validate_ohlcv(df)
    assert report.zero_or_negative_price_rows >= 1
    assert not report.passed


def test_calibration_reduces_expected_calibration_error():
    rng = np.random.default_rng(0)
    n = 2000
    y_true = rng.binomial(1, 0.5, n)
    # deliberately miscalibrated raw scores (overconfident)
    raw = np.clip(0.5 + (y_true - 0.5) * 1.6 + rng.normal(0, 0.1, n), 0, 1)
    ece_before = expected_calibration_error(raw, y_true)
    cal = ProbabilityCalibrator(method="isotonic").fit(raw, y_true)
    calibrated = cal.transform(raw)
    ece_after = expected_calibration_error(calibrated, y_true)
    assert ece_after <= ece_before


def test_prediction_rejection_when_insufficient_history():
    from return_model.inference.engine import predict_one, ModelBundle, _unavailable
    from return_model.config import EngineConfig
    from return_model.schemas import PredictionStatus

    class _Dummy:
        def fit(self, X, y): return self
        def predict(self, X): return np.zeros(len(X))
        def predict_proba(self, X): return np.tile([0.5, 0.5], (len(X), 1))

    from return_model.uncertainty.quantile import QuantileGBM, ConformalIntervals
    from return_model.inference.ood import OODDetector

    df = _synthetic_ohlcv(300)
    fe = FeatureEngine()
    feats = fe.transform(df)
    row = feats.iloc[[-1]]

    ood = OODDetector().fit(feats.dropna())
    qgbm = QuantileGBM()
    y_dummy = np.random.default_rng(0).normal(0, 0.01, len(feats.dropna()))
    qgbm.fit(feats.dropna(), y_dummy)
    conf = ConformalIntervals(_Dummy())
    conf.fit(feats.dropna(), y_dummy)
    conf.calibrate(feats.dropna(), y_dummy, alphas=(0.20,))

    bundle = ModelBundle(
        model_id="test_model", model_version="v0", data_version="d0",
        point_model=_Dummy(), prob_positive_model=_Dummy(),
        threshold_models={}, quantile_model=qgbm, conformal=conf, ood_detector=ood,
    )
    pred = predict_one(bundle, "TEST", Horizon.SWING_5D.value, row, EngineConfig(), n_train_observations=100)
    assert pred.status == PredictionStatus.UNAVAILABLE
