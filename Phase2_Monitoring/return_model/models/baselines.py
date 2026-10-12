"""
Baseline models — Section 9. Every candidate ML model must beat these,
out-of-sample, or it doesn't get promoted past EXPERIMENTAL.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, Ridge


class ZeroReturnBaseline:
    """Predicts 0 always. The null hypothesis for 'does this model add anything.'"""
    def fit(self, X, y): return self
    def predict(self, X): return np.zeros(len(X))


class HistoricalMeanBaseline:
    def __init__(self): self.mean_ = 0.0
    def fit(self, X, y):
        self.mean_ = float(np.nanmean(y))
        return self
    def predict(self, X): return np.full(len(X), self.mean_)


class HistoricalMedianBaseline:
    def __init__(self): self.median_ = 0.0
    def fit(self, X, y):
        self.median_ = float(np.nanmedian(y))
        return self
    def predict(self, X): return np.full(len(X), self.median_)


class MomentumBaseline:
    """Predicts future return = recent momentum feature, scaled by a
    single fitted coefficient (i.e. 'if it's been going up, it keeps
    going up, proportionally')."""
    def __init__(self, momentum_col: str = "mom_5"):
        self.momentum_col = momentum_col
        self.coef_ = 0.0

    def fit(self, X: pd.DataFrame, y):
        m = X[self.momentum_col].fillna(0).to_numpy().reshape(-1, 1)
        yv = np.asarray(y)
        valid = ~np.isnan(yv)
        if valid.sum() < 10:
            self.coef_ = 0.0
            return self
        reg = LinearRegression().fit(m[valid], yv[valid])
        self.coef_ = float(reg.coef_[0])
        return self

    def predict(self, X: pd.DataFrame):
        return X[self.momentum_col].fillna(0).to_numpy() * self.coef_


class LinearBaseline:
    def __init__(self, regularized: bool = True, alpha: float = 1.0):
        self.model = Ridge(alpha=alpha) if regularized else LinearRegression()

    def fit(self, X: pd.DataFrame, y):
        Xf = X.fillna(0.0)
        self.model.fit(Xf, y)
        return self

    def predict(self, X: pd.DataFrame):
        return self.model.predict(X.fillna(0.0))


BASELINE_REGISTRY = {
    "zero_return": ZeroReturnBaseline,
    "historical_mean": HistoricalMeanBaseline,
    "historical_median": HistoricalMedianBaseline,
    "momentum": MomentumBaseline,
    "linear_regularized": lambda: LinearBaseline(regularized=True),
    "linear_ols": lambda: LinearBaseline(regularized=False),
}
