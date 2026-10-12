"""
Out-of-distribution detection — Section 30.

IsolationForest fit on the training feature distribution; at inference
time, a row scoring below the calibrated threshold is flagged
`is_out_of_distribution=True`, which downstream forces LOW_TRUST status
(Section 29/30) regardless of how confident the point prediction looks.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest


class OODDetector:
    def __init__(self, contamination: float = 0.02, random_state: int = 42):
        self.model = IsolationForest(contamination=contamination, random_state=random_state, n_jobs=-1)
        self._fitted = False

    def fit(self, X_train: pd.DataFrame) -> "OODDetector":
        self.model.fit(X_train.fillna(0.0))
        self._fitted = True
        return self

    def is_out_of_distribution(self, X: pd.DataFrame) -> np.ndarray:
        if not self._fitted:
            raise RuntimeError("OODDetector must be fit on training data first")
        pred = self.model.predict(X.fillna(0.0))  # -1 = outlier, 1 = inlier
        return pred == -1

    def anomaly_score(self, X: pd.DataFrame) -> np.ndarray:
        # lower (more negative) = more anomalous
        return self.model.score_samples(X.fillna(0.0))
