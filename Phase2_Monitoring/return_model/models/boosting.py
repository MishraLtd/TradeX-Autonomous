"""
Tree-based candidate models — Section 9/21.

Uses scikit-learn's RandomForest/GradientBoosting as the guaranteed-
available baseline tier, and opportunistically wraps XGBoost/LightGBM/
CatBoost if installed (Section 36: CPU-friendly, no GPU assumption).
Quantile-capable regressors are exposed separately in
`uncertainty/quantile.py` (Section 12).
"""

from __future__ import annotations
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor

try:
    import xgboost as xgb
    _HAS_XGB = True
except ImportError:
    _HAS_XGB = False

try:
    import lightgbm as lgb
    _HAS_LGB = True
except ImportError:
    _HAS_LGB = False


def random_forest_model(random_state: int = 42, n_estimators: int = 300, max_depth: int = 6):
    return RandomForestRegressor(
        n_estimators=n_estimators, max_depth=max_depth,
        min_samples_leaf=20,  # conservative leaf size — avoid overfitting on thin panels
        n_jobs=-1, random_state=random_state,
    )


def gradient_boosting_model(random_state: int = 42, n_estimators: int = 300, loss: str = "huber"):
    """`loss='huber'` per Section 21 — more robust to fat-tailed return
    distributions than plain squared error."""
    return GradientBoostingRegressor(
        n_estimators=n_estimators, max_depth=3, learning_rate=0.03,
        loss=loss, subsample=0.8, random_state=random_state,
    )


def xgboost_model(random_state: int = 42, n_estimators: int = 400):
    if not _HAS_XGB:
        raise ImportError("xgboost not installed — pip install xgboost, or use gradient_boosting_model as fallback")
    return xgb.XGBRegressor(
        n_estimators=n_estimators, max_depth=4, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8, objective="reg:pseudohubererror",
        n_jobs=-1, random_state=random_state, tree_method="hist",
    )


def lightgbm_model(random_state: int = 42, n_estimators: int = 400):
    if not _HAS_LGB:
        raise ImportError("lightgbm not installed — pip install lightgbm, or use gradient_boosting_model as fallback")
    return lgb.LGBMRegressor(
        n_estimators=n_estimators, max_depth=4, learning_rate=0.03,
        subsample=0.8, colsample_bytree=0.8, objective="huber",
        n_jobs=-1, random_state=random_state,
    )


def available_boosting_models(random_state: int = 42) -> dict:
    models = {
        "random_forest": random_forest_model(random_state),
        "gradient_boosting_huber": gradient_boosting_model(random_state),
    }
    if _HAS_XGB:
        models["xgboost"] = xgboost_model(random_state)
    if _HAS_LGB:
        models["lightgbm"] = lightgbm_model(random_state)
    return models
