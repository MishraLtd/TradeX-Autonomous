"""
stop_loss_risk_engine.py
---------------------------
Stop-loss / worst-case risk (spec section 5.K).

Risk_i = Quantity_i * |Entry_i - Stop_i|
PortfolioStopRisk = sum(Risk_i)

Distinguishes:
  - single-position stop risk (max single contribution)
  - aggregate stop risk (sum, as % of total capital)
  - correlated stop risk (sum of stop risk within highly-correlated
    clusters — the amount that could be lost "at once" if a correlated
    group all hit stops together)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .models import Position, DataQualityReport, DataQualityState


@dataclass
class StopLossRiskResult:
    per_position_risk: Dict[str, float] = field(default_factory=dict)
    positions_missing_stop: List[str] = field(default_factory=list)
    aggregate_risk_amount: float = 0.0
    aggregate_risk_pct_of_capital: float = 0.0
    largest_single_risk_amount: float = 0.0
    largest_single_symbol: str = ""
    correlated_cluster_risk_pct: float = 0.0
    stop_loss_score: float = 0.0


def _correlated_clusters(high_corr_pairs: List[Tuple[str, str, float]]) -> List[Set[str]]:
    """Union-find style clustering of symbols connected by high-correlation pairs."""
    parent: Dict[str, str] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    for a, b, _ in high_corr_pairs:
        union(a, b)

    clusters: Dict[str, Set[str]] = {}
    for sym in parent:
        root = find(sym)
        clusters.setdefault(root, set()).add(sym)

    return [c for c in clusters.values() if len(c) > 1]


def stop_loss_risk(
    positions: List[Position],
    total_capital: float,
    warn_pct: float,
    max_pct: float,
    high_corr_pairs: List[Tuple[str, str, float]],
    report: DataQualityReport,
) -> StopLossRiskResult:
    open_positions = [p for p in positions if not p.is_proposed]
    if not open_positions:
        return StopLossRiskResult()

    per_position_risk: Dict[str, float] = {}
    missing = []

    for p in open_positions:
        r = p.stop_loss_risk_amount
        if r is None:
            missing.append(p.symbol)
        else:
            per_position_risk[p.symbol] = r

    if missing:
        report.downgrade(
            DataQualityState.LIMITED_DATA,
            f"stop-loss risk: {missing} have no stop-loss price set — their worst-case loss is UNBOUNDED "
            f"and NOT included in the aggregate figure below",
        )

    aggregate = sum(per_position_risk.values())
    aggregate_pct = aggregate / total_capital if total_capital > 0 else 0.0

    largest_symbol = max(per_position_risk, key=per_position_risk.get) if per_position_risk else ""
    largest_amount = per_position_risk.get(largest_symbol, 0.0)

    clusters = _correlated_clusters(high_corr_pairs)
    cluster_risk_amount = 0.0
    for cluster in clusters:
        cluster_risk_amount += sum(per_position_risk.get(sym, 0.0) for sym in cluster)
    cluster_risk_pct = cluster_risk_amount / total_capital if total_capital > 0 else 0.0

    if aggregate_pct <= warn_pct * 0.5:
        score = (aggregate_pct / (warn_pct * 0.5)) * 25 if warn_pct > 0 else 0.0
    elif aggregate_pct <= max_pct:
        span = max(max_pct - warn_pct * 0.5, 1e-9)
        score = 25 + (aggregate_pct - warn_pct * 0.5) / span * 50
    else:
        span = max(max_pct, 1e-9)
        score = 75 + min((aggregate_pct - max_pct) / span, 1.0) * 25

    # If unbounded (missing-stop) positions exist, nudge score up to reflect
    # the unmodeled tail risk rather than silently ignoring it.
    if missing:
        score = min(100.0, score + 10.0)

    return StopLossRiskResult(
        per_position_risk=per_position_risk,
        positions_missing_stop=missing,
        aggregate_risk_amount=aggregate,
        aggregate_risk_pct_of_capital=aggregate_pct,
        largest_single_risk_amount=largest_amount,
        largest_single_symbol=largest_symbol,
        correlated_cluster_risk_pct=cluster_risk_pct,
        stop_loss_score=max(0.0, min(100.0, score)),
    )
