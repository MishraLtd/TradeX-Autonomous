from __future__ import annotations
from dataclasses import dataclass
import numpy as np
import pandas as pd


def brier_score(y_true: np.ndarray, y_prob: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    return float(np.mean((y_prob - y_true) ** 2))


def log_loss_safe(y_true: np.ndarray, y_prob: np.ndarray, eps: float = 1e-12) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.clip(np.asarray(y_prob, dtype=float), eps, 1 - eps)
    return float(-np.mean(y_true * np.log(y_prob) + (1 - y_true) * np.log(1 - y_prob)))


@dataclass
class ReliabilityBin:
    bin_lo: float
    bin_hi: float
    predicted_mean: float
    observed_frequency: float
    count: int


def reliability_curve(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> list:
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    edges = np.linspace(0, 1, n_bins + 1)
    bins = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (y_prob >= lo) & (y_prob < hi) if i < n_bins - 1 else (y_prob >= lo) & (y_prob <= hi)
        if mask.sum() == 0:
            bins.append(ReliabilityBin(lo, hi, float("nan"), float("nan"), 0))
            continue
        bins.append(
            ReliabilityBin(
                bin_lo=lo,
                bin_hi=hi,
                predicted_mean=float(y_prob[mask].mean()),
                observed_frequency=float(y_true[mask].mean()),
                count=int(mask.sum()),
            )
        )
    return bins


def expected_calibration_error(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> float:
    bins = reliability_curve(y_true, y_prob, n_bins)
    n = len(y_true)
    ece = 0.0
    for b in bins:
        if b.count == 0:
            continue
        ece += (b.count / n) * abs(b.predicted_mean - b.observed_frequency)
    return float(ece)


def calibration_report(y_true: np.ndarray, y_prob: np.ndarray, n_bins: int = 10) -> dict:
    return {
        "brier_score": brier_score(y_true, y_prob),
        "log_loss": log_loss_safe(y_true, y_prob),
        "expected_calibration_error": expected_calibration_error(y_true, y_prob, n_bins),
        "reliability_bins": reliability_curve(y_true, y_prob, n_bins),
        "n": len(y_true),
    }
