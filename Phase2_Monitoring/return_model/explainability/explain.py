"""
Explainability — Section 27. Produces the top-contributor breakdown
shown in the output contract (Section 38 example).
"""

from __future__ import annotations
import numpy as np
import pandas as pd

try:
    import shap
    _HAS_SHAP = True
except ImportError:
    _HAS_SHAP = False


def permutation_importance_report(model, X: pd.DataFrame, y, n_repeats: int = 5, random_state: int = 42) -> pd.DataFrame:
    from sklearn.inspection import permutation_importance
    r = permutation_importance(model, X.fillna(0.0), y, n_repeats=n_repeats, random_state=random_state, n_jobs=-1)
    return pd.DataFrame({
        "feature": X.columns,
        "importance_mean": r.importances_mean,
        "importance_std": r.importances_std,
    }).sort_values("importance_mean", ascending=False).reset_index(drop=True)


def top_contributors_for_row(model, X_row: pd.DataFrame, background: pd.DataFrame, top_k: int = 5) -> list[dict]:
    """Per-prediction contributor breakdown (Section 27 worked example).
    Uses SHAP if available; otherwise falls back to a crude local
    sensitivity approximation (perturb-one-feature-at-a-time), which is
    far weaker but keeps the pipeline functional without the extra
    dependency.
    """
    if _HAS_SHAP:
        explainer = shap.TreeExplainer(model)
        shap_values = explainer.shap_values(X_row.fillna(0.0))
        vals = shap_values[0] if shap_values.ndim > 1 else shap_values
        contribs = list(zip(X_row.columns, vals))
    else:
        base_pred = model.predict(X_row.fillna(0.0))[0]
        contribs = []
        Xf = X_row.fillna(0.0)
        bg_mean = background.fillna(0.0).mean()
        for col in X_row.columns:
            perturbed = Xf.copy()
            perturbed[col] = bg_mean[col]
            new_pred = model.predict(perturbed)[0]
            contribs.append((col, base_pred - new_pred))

    contribs.sort(key=lambda x: abs(x[1]), reverse=True)
    return [{"name": name, "contribution_pct": float(val)} for name, val in contribs[:top_k]]
