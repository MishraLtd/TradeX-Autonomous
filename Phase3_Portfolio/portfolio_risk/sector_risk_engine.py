"""
sector_risk_engine.py
----------------------
Sector concentration risk (spec section 5.B).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .models import Position


@dataclass
class SectorRiskResult:
    capital_by_sector: Dict[str, float] = field(default_factory=dict)
    weight_by_sector: Dict[str, float] = field(default_factory=dict)
    largest_sector: str = ""
    largest_sector_weight: float = 0.0
    sector_hhi: float = 0.0
    sector_concentration_score: float = 0.0  # 0-100


def sector_concentration_risk(
    positions: List[Position],
    portfolio_equity: float,
    max_sector_weight: float,
    warn_sector_weight: float,
) -> SectorRiskResult:
    open_positions = [p for p in positions if not p.is_proposed]
    if not open_positions or portfolio_equity <= 0:
        return SectorRiskResult()

    capital_by_sector: Dict[str, float] = {}
    for p in open_positions:
        sector = p.sector or "UNKNOWN"
        capital_by_sector[sector] = capital_by_sector.get(sector, 0.0) + max(p.market_value, 0.0)

    weight_by_sector = {s: c / portfolio_equity for s, c in capital_by_sector.items()}
    if not weight_by_sector:
        return SectorRiskResult()

    largest_sector = max(weight_by_sector, key=weight_by_sector.get)
    largest_weight = weight_by_sector[largest_sector]
    sector_hhi = sum(w ** 2 for w in weight_by_sector.values())

    if largest_weight <= warn_sector_weight * 0.5:
        score = (largest_weight / (warn_sector_weight * 0.5)) * 25 if warn_sector_weight > 0 else 0.0
    elif largest_weight <= max_sector_weight:
        span = max(max_sector_weight - warn_sector_weight * 0.5, 1e-9)
        score = 25 + (largest_weight - warn_sector_weight * 0.5) / span * 50
    else:
        span = max(max_sector_weight, 1e-9)
        score = 75 + min((largest_weight - max_sector_weight) / span, 1.0) * 25

    score = max(0.0, min(100.0, score))

    return SectorRiskResult(
        capital_by_sector=capital_by_sector,
        weight_by_sector=weight_by_sector,
        largest_sector=largest_sector,
        largest_sector_weight=largest_weight,
        sector_hhi=sector_hhi,
        sector_concentration_score=score,
    )
