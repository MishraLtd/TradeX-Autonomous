from __future__ import annotations
from dataclasses import dataclass
import numpy as np
from scipy import stats


@dataclass
class OODDetector:
    """Fits a Gaussian reference (mean, covariance) on the training feature
    distribution and flags new observations whose Mahalanobis distance
    exceeds the chi-square quantile for the given feature dimensionality.
    Simple, transparent, and cheap — appropriate for the "low compute,
    interpretable" priority (Section 4). Can be swapped for an ensemble-
    disagreement or density-based method later without changing the
    downstream interface (an ood_score plus a boolean flag)."""

    mean_: np.ndarray = None
    inv_cov_: np.ndarray = None
    n_features_: int = 0
    chi2_threshold_: float = 0.0

    def fit(self, X: np.ndarray, chi2_percentile: float = 0.995) -> "OODDetector":
        X = np.asarray(X, dtype=float)
        X = X[~np.isnan(X).any(axis=1)]
        self.mean_ = X.mean(axis=0)
        cov = np.cov(X, rowvar=False)
        # Ridge regularization to keep the covariance invertible with a
        # modest number of training samples relative to feature count.
        cov += np.eye(cov.shape[0]) * 1e-6
        self.inv_cov_ = np.linalg.inv(cov)
        self.n_features_ = X.shape[1]
        self.chi2_threshold_ = stats.chi2.ppf(chi2_percentile, df=self.n_features_)
        return self

    def score(self, x: np.ndarray) -> float:
        """Squared Mahalanobis distance for a single observation."""
        x = np.asarray(x, dtype=float)
        if np.isnan(x).any():
            return float("inf")
        diff = x - self.mean_
        return float(diff @ self.inv_cov_ @ diff.T)

    def is_ood(self, x: np.ndarray) -> bool:
        return self.score(x) > self.chi2_threshold_

    def normalized_score(self, x: np.ndarray) -> float:
        """0..1-ish score: 1.0 at the flag threshold, >1 further out. Useful
        for feeding into `prediction_uncertainty` on a comparable scale."""
        d2 = self.score(x)
        if not np.isfinite(d2):
            return float("inf")
        return d2 / self.chi2_threshold_ if self.chi2_threshold_ > 0 else float("inf")
