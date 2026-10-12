import numpy as np
from risk_model import baselines, labels, features, calibration


def test_naive_baseline_matches_frequency():
    loss = np.array([1, 0, 1, 1, 0, 0, 0, 1])
    stop = np.array([0, 0, 1, 1, 0, 0, 0, 1])
    b = baselines.NaiveFrequencyBaseline().fit(loss, stop)
    assert abs(b.p_loss_ - 0.5) < 1e-9
    assert abs(b.p_stop_hit_ - 0.375) < 1e-9
    preds = b.predict(3)
    assert (preds["probability_of_loss"] == 0.5).all()


def test_monte_carlo_stop_target_race_sane_bounds():
    result = baselines.simulate_stop_target_race(
        volatility_annual_pct=30.0,
        horizon_bars=5,
        stop_distance_pct=1.0,
        target_distance_pct=1.0,
        n_paths=5000,
    )
    # symmetric stop/target distances with zero drift => roughly symmetric
    # probabilities (allow simulation noise)
    assert abs(result["probability_stop_before_target"] - result["probability_target_before_stop"]) < 0.05
    assert 0 <= result["probability_of_loss"] <= 1
    assert result["mae_p90_pct"] <= result["mae_p50_pct"]  # p90 more negative (worse)


def test_calibrated_logistic_pipeline_fits_and_is_calibrated_reasonably(ohlcv):
    feat = features.build_feature_frame(ohlcv)
    lf = labels.build_label_frame(ohlcv, horizon_bars=5, stop_distance_pct=1.5, target_distance_pct=3.0)
    merged = lf.merge(feat, left_on="entry_idx", right_index=True, suffixes=("", "_feat"))
    merged = merged.dropna(subset=features.FEATURE_COLUMNS)
    assert len(merged) > 50

    X = merged[features.FEATURE_COLUMNS].to_numpy()
    y = merged["stop_hit"].astype(int).to_numpy()

    model = baselines.build_calibrated_logistic()
    model.fit(X, y)
    probs = model.predict_proba(X)[:, 1]

    report = calibration.calibration_report(y, probs, n_bins=5)
    assert 0 <= report["brier_score"] <= 1
    assert report["n"] == len(y)
