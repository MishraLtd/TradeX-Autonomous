"""
liquidity_risk_engine.py
--------------------------
Liquidity risk (spec section 16), consuming liquidity metrics from
FILTER2.0 (average traded value) rather than recomputing them.

Flags position_size / typical_traded_value ratios above configurable
thresholds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Position, DataQualityReport, DataQualityState


@dataclass
class LiquidityRiskResult:
    ratios: Dict[str, Optional[float]] = field(default_factory=dict)
    worst_symbol: str = ""
    worst_ratio: float = 0.0
    illiquid_positions: List[str] = field(default_factory=list)
    liquidity_score: float = 0.0


def liquidity_risk(
    positions: List[Position],
    warn_ratio: float,
    max_ratio: float,
    report: DataQualityReport,
) -> LiquidityRiskResult:
    open_positions = [p for p in positions if not p.is_proposed]
    if not open_positions:
        return LiquidityRiskResult()

    ratios: Dict[str, Optional[float]] = {}
    illiquid = []
    missing = []

    for p in open_positions:
        if p.avg_traded_value and p.avg_traded_value > 0:
            ratio = p.market_value / p.avg_traded_value
            ratios[p.symbol] = ratio
            if ratio >= max_ratio:
                illiquid.append(p.symbol)
        else:
            ratios[p.symbol] = None
            missing.append(p.symbol)

    if missing:
        report.downgrade(
            DataQualityState.LIMITED_DATA,
            f"liquidity: no avg_traded_value for {missing} — liquidity risk for these positions cannot be assessed",
        )

    valid_ratios = {s: r for s, r in ratios.items() if r is not None}
    if not valid_ratios:
        return LiquidityRiskResult(ratios=ratios, illiquid_positions=illiquid)

    worst_symbol = max(valid_ratios, key=valid_ratios.get)
    worst_ratio = valid_ratios[worst_symbol]

    if worst_ratio <= warn_ratio * 0.5:
        score = (worst_ratio / (warn_ratio * 0.5)) * 25 if warn_ratio > 0 else 0.0
    elif worst_ratio <= max_ratio:
        span = max(max_ratio - warn_ratio * 0.5, 1e-9)
        score = 25 + (worst_ratio - warn_ratio * 0.5) / span * 50
    else:
        span = max(max_ratio, 1e-9)
        score = 75 + min((worst_ratio - max_ratio) / span, 1.0) * 25

    return LiquidityRiskResult(
        ratios=ratios,
        worst_symbol=worst_symbol,
        worst_ratio=worst_ratio,
        illiquid_positions=illiquid,
        liquidity_score=max(0.0, min(100.0, score)),
    )
