"""
Scenario analysis (spec §28) — consumes the Cost Model's OPTIMISTIC / BASE /
CONSERVATIVE scenarios rather than re-deriving them. Produces a resilience
multiplier that penalizes opportunities whose apparent quality collapses
under the conservative case (spec §28's "fragile opportunity" example).

Deliberately NOT full Monte Carlo (spec §29): with only three discrete
Cost-Model scenarios available in Phase 1, a closed-form resilience ratio
is sufficient and far more auditable than simulating a distribution we
don't actually have inputs to parameterize honestly. Monte Carlo is left
as a documented Phase-2+ upgrade once the Return/Risk Models expose full
parametric or empirical distributions.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from .config import OpportunityModelConfig
from .normalization import safe_ratio, clamp
from .schemas import OpportunityCandidate, ScenarioScores


def compute_scenario_scores(
    candidate: OpportunityCandidate,
    base_score: Decimal,
    config: OpportunityModelConfig,
) -> ScenarioScores:
    """
    We do not re-run the full component pipeline for optimistic/conservative
    scenarios (that would require the Return/Risk models to also expose
    scenario-conditioned outputs, which spec §28 does not assume). Instead
    we scale `base_score` by the ratio of each scenario's net return to the
    base scenario's net return, which is a reasonable, cheap, and auditable
    proxy given only the Cost Model's scenario outputs are guaranteed.
    """
    cost = candidate.cost_prediction
    base_net = cost.base.expected_net_return_pct

    def scaled(scenario) -> Optional[Decimal]:
        if scenario is None or base_net == 0:
            return None
        ratio = safe_ratio(scenario.expected_net_return_pct, base_net)
        ratio = clamp(ratio, Decimal("0"), Decimal("2"))  # cap upside credit
        return clamp(base_score * ratio, Decimal("0"), Decimal("100")).quantize(Decimal("0.01"))

    optimistic_score = scaled(cost.optimistic)
    conservative_score = scaled(cost.conservative)

    resilience = _resilience_multiplier(base_score, conservative_score, config).quantize(Decimal("0.001"))

    return ScenarioScores(
        optimistic=optimistic_score,
        base=base_score.quantize(Decimal("0.01")),
        conservative=conservative_score,
        scenario_resilience=resilience,
    )


def _resilience_multiplier(
    base_score: Decimal,
    conservative_score: Optional[Decimal],
    config: OpportunityModelConfig,
) -> Decimal:
    sc = config.scenarios
    if conservative_score is None or base_score == 0:
        # No conservative scenario supplied: neither reward nor punish,
        # but do not manufacture confidence either - apply a small default
        # haircut to reflect the missing robustness evidence.
        return Decimal("0.9")

    ratio = safe_ratio(conservative_score, base_score)
    if ratio >= sc.min_conservative_to_base_ratio:
        # Linearly map [min_ratio, 1.0] -> [floor_mult, ceiling_mult]
        span = Decimal("1.0") - sc.min_conservative_to_base_ratio
        progress = clamp((ratio - sc.min_conservative_to_base_ratio) / span, Decimal("0"), Decimal("1"))
        return sc.resilience_floor_multiplier + progress * (
            sc.resilience_ceiling_multiplier - sc.resilience_floor_multiplier
        )
    else:
        # Below threshold: floors out at the resilience floor multiplier,
        # scaled further down proportionally to how far below threshold.
        deficit = clamp(sc.min_conservative_to_base_ratio - ratio, Decimal("0"), sc.min_conservative_to_base_ratio)
        extra_penalty = deficit / sc.min_conservative_to_base_ratio * Decimal("0.3")
        return clamp(sc.resilience_floor_multiplier - extra_penalty, Decimal("0.25"), sc.resilience_floor_multiplier)
