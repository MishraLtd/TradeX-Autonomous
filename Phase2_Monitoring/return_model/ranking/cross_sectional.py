"""
Cross-sectional ranking and Top-K evaluation — Sections 17, 25.
"""

from __future__ import annotations
import pandas as pd
import numpy as np


def rank_candidates_at_timestamp(
    predictions: pd.DataFrame,
    score_col: str = "expected_net_return_pct",
    prob_col: str = "probability_positive",
    confidence_col: str = "confidence",
) -> pd.DataFrame:
    """Rank all candidate symbols at a single decision timestamp.
    `predictions` must already be filtered to one timestamp (one row
    per symbol). Composite rank blends expected net return, direction
    probability, and confidence — weights are a starting default and
    should be tuned against realized Top-K performance."""
    df = predictions.copy()
    df["rank_score"] = (
        0.6 * df[score_col].rank(pct=True)
        + 0.25 * df[prob_col].rank(pct=True)
        + 0.15 * df[confidence_col].rank(pct=True)
    )
    df = df.sort_values("rank_score", ascending=False).reset_index(drop=True)
    df["rank"] = np.arange(1, len(df) + 1)
    return df


def top_k_backtest_summary(
    ranked_by_timestamp: dict[pd.Timestamp, pd.DataFrame],
    realized_return_col: str,
    k_values=(1, 3, 5, 10),
) -> pd.DataFrame:
    """For each K, simulate 'take the top-K ranked candidates each
    decision period' and summarize realized outcomes (Section 25)."""
    rows = []
    for k in k_values:
        period_returns = []
        hits = []
        for ts, df in ranked_by_timestamp.items():
            top = df[df["rank"] <= k]
            if top.empty:
                continue
            period_returns.append(top[realized_return_col].mean())
            hits.append((top[realized_return_col] > 0).mean())
        if not period_returns:
            continue
        arr = np.array(period_returns)
        rows.append({
            "k": k,
            "n_periods": len(arr),
            "avg_net_return_pct": float(np.mean(arr)),
            "median_net_return_pct": float(np.median(arr)),
            "std_net_return_pct": float(np.std(arr)),
            "hit_rate": float(np.mean(hits)),
            "worst_period_pct": float(np.min(arr)),
            "best_period_pct": float(np.max(arr)),
            "max_drawdown_proxy_pct": float(np.min(np.cumsum(arr) - np.maximum.accumulate(np.cumsum(arr)))),
        })
    return pd.DataFrame(rows)
