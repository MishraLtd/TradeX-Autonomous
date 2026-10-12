"""
Evaluation metrics — Section 24. Split into ML metrics (does the model
fit the data) and trading metrics (does it make money after cost) —
Section 24 explicitly requires reporting both, and Section 41/42 require
never treating positive backtest P&L alone as proof the model works.
"""

from __future__ import annotations
import numpy as np
import pandas as pd
from sklearn.metrics import (
    mean_absolute_error, mean_squared_error, r2_score,
    precision_score, recall_score, f1_score, roc_auc_score,
    average_precision_score, log_loss,
)


def ml_regression_metrics(y_true, y_pred) -> dict:
    y_true, y_pred = np.asarray(y_true), np.asarray(y_pred)
    mask = ~np.isnan(y_true) & ~np.isnan(y_pred)
    y_true, y_pred = y_true[mask], y_pred[mask]
    if len(y_true) == 0:
        return {}
    directional_acc = float(np.mean(np.sign(y_true) == np.sign(y_pred)))
    return {
        "mae": float(mean_absolute_error(y_true, y_pred)),
        "rmse": float(np.sqrt(mean_squared_error(y_true, y_pred))),
        "r2": float(r2_score(y_true, y_pred)) if len(set(y_true)) > 1 else float("nan"),
        "directional_accuracy": directional_acc,
        "n": int(len(y_true)),
    }


def ml_classification_metrics(y_true, y_prob, threshold: float = 0.5) -> dict:
    y_true, y_prob = np.asarray(y_true), np.asarray(y_prob)
    mask = ~np.isnan(y_true) & ~np.isnan(y_prob)
    y_true, y_prob = y_true[mask], y_prob[mask]
    if len(y_true) == 0 or len(set(y_true)) < 2:
        return {}
    y_pred = (y_prob >= threshold).astype(int)
    out = {
        "precision": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1": float(f1_score(y_true, y_pred, zero_division=0)),
        "roc_auc": float(roc_auc_score(y_true, y_prob)),
        "pr_auc": float(average_precision_score(y_true, y_prob)),
        "log_loss": float(log_loss(y_true, y_prob, labels=[0, 1])),
        "n": int(len(y_true)),
    }
    return out


def trading_metrics(net_returns_pct: pd.Series) -> dict:
    """`net_returns_pct` = realized net return per executed trade, already
    after friction (Section 42 — cost-aware evaluation)."""
    r = pd.Series(net_returns_pct).dropna()
    if len(r) == 0:
        return {}
    wins = r[r > 0]
    losses = r[r <= 0]
    cum = r.cumsum()
    drawdown = (cum - cum.cummax()).min()
    win_rate = len(wins) / len(r)
    profit_factor = wins.sum() / abs(losses.sum()) if losses.sum() != 0 else float("inf")
    expectancy = r.mean()
    sharpe = r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else float("nan")
    downside = r[r < 0].std()
    sortino = r.mean() / downside * np.sqrt(252) if downside and downside > 0 else float("nan")
    return {
        "n_trades": int(len(r)),
        "win_rate": float(win_rate),
        "profit_factor": float(profit_factor),
        "expectancy_pct": float(expectancy),
        "avg_win_pct": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_pct": float(losses.mean()) if len(losses) else 0.0,
        "max_drawdown_pct": float(drawdown),
        "sharpe_annualized": float(sharpe),
        "sortino_annualized": float(sortino),
        "total_return_pct": float(cum.iloc[-1]),
    }


def compare_zero_vs_realistic_cost(gross_returns_pct: pd.Series, net_returns_pct: pd.Series) -> dict:
    """Section 42: show how much apparent alpha disappears after costs."""
    gross_stats = trading_metrics(gross_returns_pct)
    net_stats = trading_metrics(net_returns_pct)
    alpha_erosion = (gross_stats.get("total_return_pct", 0) - net_stats.get("total_return_pct", 0))
    return {"zero_cost": gross_stats, "realistic_cost": net_stats, "alpha_erosion_pct": alpha_erosion}


def bootstrap_confidence_interval(returns_pct: pd.Series, n_boot: int = 2000, ci: float = 0.95, seed: int = 42) -> dict:
    """Section 41: is the apparent edge statistically significant, or
    within the noise band of a bootstrap resample?"""
    rng = np.random.default_rng(seed)
    r = pd.Series(returns_pct).dropna().to_numpy()
    if len(r) == 0:
        return {}
    means = np.array([rng.choice(r, size=len(r), replace=True).mean() for _ in range(n_boot)])
    lo_q, hi_q = (1 - ci) / 2, 1 - (1 - ci) / 2
    return {
        "mean": float(r.mean()),
        "ci_lower": float(np.quantile(means, lo_q)),
        "ci_upper": float(np.quantile(means, hi_q)),
        "excludes_zero": bool(np.quantile(means, lo_q) > 0 or np.quantile(means, hi_q) < 0),
    }
