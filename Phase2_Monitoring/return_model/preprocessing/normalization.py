"""
Normalization — Section 16.

Two supported modes, both leakage-safe by construction:

1. RollingNormalizer: at row t, normalize using only stats computed
   over [t - window, t - 1] (i.e. shift(1) before the rolling stat).
   No global fit; safe to apply directly to any historical row without
   a separate "fit" step.

2. TrainOnlyNormalizer: fit ONCE on a training slice (robust median/IQR
   scaling), then applied unchanged to validation/test. This is closer
   to how the inference-time model will actually behave (fixed scaler
   deployed to PRODUCTION), so it's the default for model input.

Cross-sectional normalization (rank/z-score across the universe at a
single timestamp) is provided separately since it operates on a panel,
not a single symbol's time series.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


class RollingNormalizer:
    def __init__(self, window: int = 60, min_periods: int = 20):
        self.window = window
        self.min_periods = min_periods

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        # shift(1) is the leakage guard: stats for row t use rows < t only.
        mean = X.shift(1).rolling(self.window, min_periods=self.min_periods).mean()
        std = X.shift(1).rolling(self.window, min_periods=self.min_periods).std()
        return (X - mean) / std.replace(0, np.nan)


class TrainOnlyNormalizer:
    """Robust (median / IQR) scaler fit once on a training slice."""

    def __init__(self):
        self.median_: pd.Series | None = None
        self.iqr_: pd.Series | None = None

    def fit(self, X_train: pd.DataFrame) -> "TrainOnlyNormalizer":
        self.median_ = X_train.median()
        q75 = X_train.quantile(0.75)
        q25 = X_train.quantile(0.25)
        self.iqr_ = (q75 - q25).replace(0, np.nan)
        return self

    def transform(self, X: pd.DataFrame) -> pd.DataFrame:
        if self.median_ is None or self.iqr_ is None:
            raise RuntimeError("TrainOnlyNormalizer.fit() must be called on the training slice first")
        return (X - self.median_) / self.iqr_

    def fit_transform(self, X_train: pd.DataFrame) -> pd.DataFrame:
        return self.fit(X_train).transform(X_train)


def cross_sectional_zscore(panel: pd.DataFrame, timestamp_col: str, value_col: str) -> pd.Series:
    """Z-score value_col across all symbols within each timestamp group.
    Uses only that timestamp's cross-section -> no temporal leakage."""
    def _z(s: pd.Series) -> pd.Series:
        std = s.std()
        return (s - s.mean()) / std if std and not np.isnan(std) and std != 0 else s * 0.0
    return panel.groupby(timestamp_col)[value_col].transform(_z)
