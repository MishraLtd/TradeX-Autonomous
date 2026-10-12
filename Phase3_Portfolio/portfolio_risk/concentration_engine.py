"""
concentration_engine.py
------------------------
Position concentration risk (spec section 5.A).

Metrics:
  - largest_position_weight
  - top2_weight, top5_weight
  - hhi (Herfindahl-Hirschman Index, computed on weights in [0,1], so
    max = 1.0 for a single-position portfolio, min -> 0 as N grows for
    equal weights)
  - concentration_score (0-100, normalized against configurable limits)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .models import Position


@dataclass
class ConcentrationResult:
    weights: Dict[str, float] = field(default_factory=dict)
    largest_position_weight: float = 0.0
    largest_position_symbol: str = ""
    top2_weight: float = 0.0
    top5_weight: float = 0.0
    hhi: float = 0.0
    concentration_score: float = 0.0  # 0-100


def compute_weights(positions: List[Position], portfolio_equity: float) -> Dict[str, float]:
    if portfolio_equity <= 0:
        return {p.symbol: 0.0 for p in positions}
    return {p.symbol: max(p.market_value, 0.0) / portfolio_equity for p in positions}


def concentration_risk(
    positions: List[Position],
    portfolio_equity: float,
    max_position_weight: float,
    warn_position_weight: float,
) -> ConcentrationResult:
    open_positions = [p for p in positions if not p.is_proposed]

    if not open_positions or portfolio_equity <= 0:
        return ConcentrationResult()

    weights = compute_weights(open_positions, portfolio_equity)
    sorted_weights = sorted(weights.values(), reverse=True)

    largest = sorted_weights[0] if sorted_weights else 0.0
    largest_symbol = max(weights, key=weights.get) if weights else ""
    top2 = sum(sorted_weights[:2])
    top5 = sum(sorted_weights[:5])
    hhi = sum(w ** 2 for w in sorted_weights)

    # Score: piecewise-linear ramp from 0 at "warn" threshold's midpoint to
    # 100 at 2x the max threshold. Anything below half the warn threshold is
    # treated as negligible risk.
    if largest <= warn_position_weight * 0.5:
        score = (largest / (warn_position_weight * 0.5)) * 25 if warn_position_weight > 0 else 0.0
    elif largest <= max_position_weight:
        span = max(max_position_weight - warn_position_weight * 0.5, 1e-9)
        score = 25 + (largest - warn_position_weight * 0.5) / span * 50
    else:
        span = max(max_position_weight, 1e-9)
        score = 75 + min((largest - max_position_weight) / span, 1.0) * 25

    score = max(0.0, min(100.0, score))

    return ConcentrationResult(
        weights=weights,
        largest_position_weight=largest,
        largest_position_symbol=largest_symbol,
        top2_weight=top2,
        top5_weight=top5,
        hhi=hhi,
        concentration_score=score,
    )
