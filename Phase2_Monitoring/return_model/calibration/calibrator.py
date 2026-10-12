"""
Probability calibration — Section 13.

Wraps any classifier producing raw probability scores and recalibrates
them via Platt scaling (logistic) or isotonic regression, fit ONLY on
a held-out calibration slice (never the training slice, never the
final test slice — Section 13 + Section 40 anti-overfitting rule).
"""

from __future__ import annotations
import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.isotonic import IsotonicRegression


class ProbabilityCalibrator:
    def __init__(self, method: str = "isotonic"):
        assert method in ("isotonic", "platt")
        self.method = method
        self._model = None

    def fit(self, raw_probs: np.ndarray, y_true: np.ndarray) -> "ProbabilityCalibrator":
        raw_probs = np.asarray(raw_probs).reshape(-1, 1)
        y_true = np.asarray(y_true)
        if self.method == "isotonic":
            self._model = IsotonicRegression(out_of_bounds="clip")
            self._model.fit(raw_probs.ravel(), y_true)
        else:
            self._model = LogisticRegression()
            self._model.fit(raw_probs, y_true)
        return self

    def transform(self, raw_probs: np.ndarray) -> np.ndarray:
        raw_probs = np.asarray(raw_probs)
        if self._model is None:
            raise RuntimeError("Calibrator not fit yet")
        if self.method == "isotonic":
            return self._model.predict(raw_probs)
        return self._model.predict_proba(raw_probs.reshape(-1, 1))[:, 1]


def reliability_diagram_data(raw_probs: np.ndarray, y_true: np.ndarray, n_bins: int = 10):
    """Returns (bin_centers, observed_freq, predicted_mean, bin_counts)
    for plotting a reliability diagram and computing calibration error."""
    raw_probs = np.asarray(raw_probs)
    y_true = np.asarray(y_true)
    bins = np.linspace(0, 1, n_bins + 1)
    bin_centers, observed, predicted, counts = [], [], [], []
    for i in range(n_bins):
        mask = (raw_probs >= bins[i]) & (raw_probs < bins[i + 1] if i < n_bins - 1 else raw_probs <= bins[i + 1])
        if mask.sum() == 0:
            continue
        bin_centers.append((bins[i] + bins[i + 1]) / 2)
        observed.append(float(y_true[mask].mean()))
        predicted.append(float(raw_probs[mask].mean()))
        counts.append(int(mask.sum()))
    return bin_centers, observed, predicted, counts


def expected_calibration_error(raw_probs: np.ndarray, y_true: np.ndarray, n_bins: int = 10) -> float:
    _, observed, predicted, counts = reliability_diagram_data(raw_probs, y_true, n_bins)
    total = sum(counts)
    if total == 0:
        return float("nan")
    return sum(c * abs(o - p) for c, o, p in zip(counts, observed, predicted)) / total


def brier_score(raw_probs: np.ndarray, y_true: np.ndarray) -> float:
    raw_probs = np.asarray(raw_probs)
    y_true = np.asarray(y_true)
    return float(np.mean((raw_probs - y_true) ** 2))
