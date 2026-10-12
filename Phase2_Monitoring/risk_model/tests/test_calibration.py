import numpy as np
from risk_model import calibration


def test_perfect_calibration_gives_near_zero_ece():
    rng = np.random.default_rng(0)
    probs = rng.uniform(0, 1, size=5000)
    y = rng.binomial(1, probs)
    ece = calibration.expected_calibration_error(y, probs, n_bins=10)
    assert ece < 0.05  # sampling noise only


def test_badly_calibrated_model_flagged():
    # model always predicts 0.9 regardless of true rate ~0.2
    rng = np.random.default_rng(0)
    y = rng.binomial(1, 0.2, size=2000)
    probs = np.full(2000, 0.9)
    ece = calibration.expected_calibration_error(y, probs, n_bins=10)
    assert ece > 0.5


def test_brier_score_bounds():
    y = np.array([1, 0, 1, 0])
    probs = np.array([1.0, 0.0, 1.0, 0.0])
    assert calibration.brier_score(y, probs) == 0.0
    probs_bad = np.array([0.0, 1.0, 0.0, 1.0])
    assert calibration.brier_score(y, probs_bad) == 1.0
