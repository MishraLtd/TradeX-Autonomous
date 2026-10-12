"""
Distribution / uncertainty estimation — Sections 12, 28.

Two complementary approaches:

1. QuantileGBM: separate GradientBoostingRegressor per quantile
   (native `loss="quantile"` support). Gives quantiles_pct directly.

2. ConformalIntervals: split-conformal wrapper around ANY point
   predictor. Calibrated on a held-out (never-trained-on) slice, so
   the resulting interval has a distribution-free finite-sample
   coverage guarantee — this is what "Confidence = 0.78" in the
   output contract should be traceable to, not a vibe.

Both must be fit only on training data and calibrated only on a
calibration slice that is itself walk-forward / purged relative to
whatever it will be evaluated against (Section 13).
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor


class QuantileGBM:
    def __init__(self, quantiles=(0.10, 0.25, 0.50, 0.75, 0.90), random_state: int = 42):
        self.quantiles = quantiles
        self.random_state = random_state
        self.models_: dict[float, GradientBoostingRegressor] = {}

    def fit(self, X: pd.DataFrame, y) -> "QuantileGBM":
        Xf = X.fillna(0.0)
        for q in self.quantiles:
            m = GradientBoostingRegressor(
                loss="quantile", alpha=q, n_estimators=250, max_depth=3,
                learning_rate=0.03, subsample=0.8, random_state=self.random_state,
            )
            m.fit(Xf, y)
            self.models_[q] = m
        return self

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        Xf = X.fillna(0.0)
        preds = {f"q{int(q*100)}": self.models_[q].predict(Xf) for q in self.quantiles}
        out = pd.DataFrame(preds, index=X.index)
        # enforce monotonicity across quantiles row-wise (crossing quantiles
        # are a known artifact of independently-fit quantile models)
        cols = sorted(out.columns, key=lambda c: int(c[1:]))
        out[cols] = np.sort(out[cols].to_numpy(), axis=1)
        return out


class ConformalIntervals:
    """Split-conformal prediction interval around a fitted point predictor.

    Usage:
        cp = ConformalIntervals(base_model)
        cp.fit(X_train, y_train)
        cp.calibrate(X_calib, y_calib)   # X_calib/y_calib NOT used in fit()
        lo, hi = cp.predict_interval(X_test, alpha=0.20)  # 80% interval
    """

    def __init__(self, base_model):
        self.base_model = base_model
        self.residual_quantiles_: dict[float, float] = {}

    def fit(self, X, y):
        Xf = X.fillna(0.0) if hasattr(X, "fillna") else X
        self.base_model.fit(Xf, y)
        return self

    def calibrate(self, X_calib, y_calib, alphas=(0.10, 0.20, 0.50)):
        Xf = X_calib.fillna(0.0) if hasattr(X_calib, "fillna") else X_calib
        preds = self.base_model.predict(Xf)
        residuals = np.abs(np.asarray(y_calib) - preds)
        n = len(residuals)
        for alpha in alphas:
            # conformal quantile with finite-sample correction
            level = min(1.0, np.ceil((n + 1) * (1 - alpha)) / n)
            self.residual_quantiles_[alpha] = float(np.quantile(residuals, level))
        return self

    def predict_interval(self, X, alpha: float = 0.20):
        if alpha not in self.residual_quantiles_:
            raise ValueError(f"Not calibrated for alpha={alpha}. Call .calibrate() with this alpha first.")
        Xf = X.fillna(0.0) if hasattr(X, "fillna") else X
        point = self.base_model.predict(Xf)
        width = self.residual_quantiles_[alpha]
        return point - width, point + width

    def confidence_score(self, X, alpha: float = 0.20, scale: float = 0.05) -> np.ndarray:
        """Maps interval half-width to a bounded [0,1] 'confidence' proxy:
        narrower interval (relative to `scale`, a typical return magnitude
        for the horizon) -> higher confidence. This is a monotonic
        transform for display purposes; the ACTUAL statistical guarantee
        is the conformal interval itself, not this scalar."""
        lo, hi = self.predict_interval(X, alpha=alpha)
        half_width = (hi - lo) / 2.0
        return np.clip(1.0 - (half_width / scale), 0.0, 1.0)
