"""
scoring_engine.py
--------------------
Combines all sub-engine scores into a single portfolio_risk_score (0-100)
using configurable, documented weights (spec section 7) — NOT a blind
average.

Also implements risk classification (spec section 8), which considers both:
  1. absolute portfolio risk (the composite score)
  2. risk relative to available capital (capital tier context)

A tiny portfolio and a large one with similar raw volatility are NOT
automatically classified identically: at very small capital, the
classification includes a note when diversification-driven components
(concentration/sector/correlation) are elevated for reasons that may be
economically unavoidable (spec section 14), without ever lowering the
numeric score itself — the note contextualizes, it does not hide risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from .config import RiskConfig, capital_tier, MIN_POSITIONS_FOR_FULL_DIVERSIFICATION


@dataclass
class ScoringResult:
    sub_scores: Dict[str, float] = field(default_factory=dict)
    weights_used: Dict[str, float] = field(default_factory=dict)
    portfolio_risk_score: float = 0.0
    risk_class: str = "LOW"
    capital_tier: str = "STANDARD"
    classification_notes: List[str] = field(default_factory=list)


def classify(score: float, thresholds: Dict[str, tuple]) -> str:
    for name, (lo, hi) in thresholds.items():
        if lo <= score < hi:
            return name
    return "CRITICAL"


def compute_portfolio_risk_score(
    config: RiskConfig,
    sub_scores: Dict[str, float],
    total_capital: float,
    num_positions: int,
) -> ScoringResult:
    weights = config.score_weights
    weight_sum = sum(weights.values())
    if abs(weight_sum - 1.0) > 1e-6:
        # Defensive normalization — weights must sum to 1.0, but never crash
        # the model over a config typo.
        weights = {k: v / weight_sum for k, v in weights.items()}

    composite = 0.0
    for category, weight in weights.items():
        composite += weight * sub_scores.get(category, 0.0)

    composite = max(0.0, min(100.0, composite))
    risk_class = classify(composite, config.risk_class_thresholds)
    tier = capital_tier(total_capital)

    notes: List[str] = []
    if tier in ("MICRO", "VERY_SMALL") and num_positions < MIN_POSITIONS_FOR_FULL_DIVERSIFICATION:
        elevated_diversification_risk = (
            sub_scores.get("concentration", 0) > 50
            or sub_scores.get("sector", 0) > 50
            or sub_scores.get("correlation", 0) > 50
        )
        if elevated_diversification_risk:
            notes.append(
                f"Capital tier is {tier}; full diversification across "
                f">= {MIN_POSITIONS_FOR_FULL_DIVERSIFICATION} uncorrelated positions may be economically "
                f"infeasible after transaction costs at this capital level. Elevated concentration/sector/"
                f"correlation scores are reported at full severity (not discounted) — this note is context, "
                f"not a risk reduction."
            )

    return ScoringResult(
        sub_scores=sub_scores,
        weights_used=weights,
        portfolio_risk_score=composite,
        risk_class=risk_class,
        capital_tier=tier,
        classification_notes=notes,
    )
