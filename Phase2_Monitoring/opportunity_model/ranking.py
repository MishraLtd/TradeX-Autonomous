"""
Level 3 — Relative Ranking (spec §19, §23, §36).

Ranks the ELIGIBLE assessments from a batch of candidates, assigns
absolute_rank (by raw opportunity_score) and relative_rank (identical in
Phase 1, since we have only one scoring pass per cycle - the two fields
are kept distinct in the schema for Phase 2, where absolute score and a
percentile-based relative rank against a rolling historical distribution
may diverge - see README §25).

Implements mandatory abstention (spec §35): if zero candidates are
eligible, or all eligible candidates score below `minimum_score`, the
function returns a RankedOpportunitySet with no_trade=True rather than
forcing a ranking of weak opportunities.

Also implements minimum-score-margin stability (spec §26): candidates
whose scores differ by less than `stability.minimum_material_score_change`
are treated as tied for ranking-explanation purposes (their relative
order may still be reported, but the explanation notes the tie rather
than implying one is meaningfully better).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List

from .config import OpportunityModelConfig, DEFAULT_CONFIG
from .exceptions import DataIncompleteError, InvalidInputError
from .schemas import (
    OpportunityCandidate,
    OpportunityAssessment,
    EligibilityStatus,
    RejectionReason,
    RankedOpportunitySet,
)
from .scoring import evaluate_opportunity, MODEL_VERSION


MINIMUM_SCORE_TO_RANK = Decimal("55")
"""A candidate can pass every hard gate yet still be a mediocre opportunity
(gates check "acceptable", not "attractive"). This threshold is what makes
abstention (spec §35) possible even when candidates are technically
eligible - it is intentionally separate from, and typically higher than,
any single gate."""


def rank_opportunities(
    candidates: List[OpportunityCandidate],
    config: OpportunityModelConfig = DEFAULT_CONFIG,
    now: datetime | None = None,
    minimum_score_to_rank: Decimal = MINIMUM_SCORE_TO_RANK,
) -> RankedOpportunitySet:
    now = now or datetime.now(timezone.utc)
    assessments: List[OpportunityAssessment] = []

    for candidate in candidates:
        try:
            assessment = evaluate_opportunity(candidate, config=config, now=now)
        except (DataIncompleteError, InvalidInputError) as exc:
            assessment = _incomplete_assessment(candidate, str(exc), config, now)
        assessments.append(assessment)

    eligible = [a for a in assessments if a.eligibility_status == EligibilityStatus.ELIGIBLE]
    rankable = [a for a in eligible if a.opportunity_score is not None and a.opportunity_score >= minimum_score_to_rank]

    rankable.sort(key=lambda a: a.opportunity_score, reverse=True)

    ranked_assessments: List[OpportunityAssessment] = []
    prev_score = None
    for idx, a in enumerate(rankable, start=1):
        explanation = _ranking_explanation(a, idx, len(rankable), prev_score, config)
        ranked_assessments.append(
            a.model_copy(update={
                "absolute_rank": idx,
                "relative_rank": idx,
                "ranking_explanation": explanation,
            })
        )
        prev_score = a.opportunity_score

    no_trade = len(ranked_assessments) == 0
    no_trade_reason = None
    if no_trade:
        if len(eligible) == 0:
            no_trade_reason = "NO_ELIGIBLE_CANDIDATES: all candidates failed one or more hard gates."
        else:
            no_trade_reason = (
                f"NO_QUALIFYING_OPPORTUNITY: {len(eligible)} candidate(s) passed hard gates but "
                f"none reached the minimum rankable score ({minimum_score_to_rank})."
            )

    top_k = ranked_assessments[: config.top_k_max]

    # Rebuild the full assessment list, substituting ranked versions in place.
    ranked_by_id = {a.opportunity_id: a for a in ranked_assessments}
    all_assessments = [ranked_by_id.get(a.opportunity_id, a) for a in assessments]

    return RankedOpportunitySet(
        generated_at=now,
        total_candidates=len(candidates),
        eligible_count=len(eligible),
        rejected_count=len(candidates) - len(eligible),
        top_opportunities=top_k,
        all_assessments=all_assessments,
        no_trade=no_trade,
        no_trade_reason=no_trade_reason,
    )


def _ranking_explanation(
    assessment: OpportunityAssessment,
    rank: int,
    total: int,
    prev_score,
    config: OpportunityModelConfig,
) -> str:
    tie_note = ""
    if prev_score is not None:
        delta = prev_score - assessment.opportunity_score
        if delta < config.stability.minimum_material_score_change:
            tie_note = (
                f" (statistically indistinguishable from rank {rank - 1}; "
                f"score delta {delta} is below the minimum material change "
                f"threshold of {config.stability.minimum_material_score_change})"
            )
    band = assessment.score_band
    return (
        f"Rank #{rank} of {total} eligible candidates. Score {assessment.opportunity_score} "
        f"({band}). Net return {assessment.expected_net_return_pct}%, "
        f"PoP {assessment.probability_of_profit}, scenario resilience "
        f"{assessment.scenario_scores.scenario_resilience if assessment.scenario_scores else 'n/a'}."
        f"{tie_note}"
    )


def _incomplete_assessment(
    candidate: OpportunityCandidate, detail: str, config: OpportunityModelConfig, now: datetime
) -> OpportunityAssessment:
    return OpportunityAssessment(
        opportunity_id=f"incomplete-{candidate.symbol}-{now.timestamp()}",
        symbol=candidate.symbol,
        timestamp=candidate.timestamp,
        trade_type=candidate.trade_type,
        strategy=candidate.strategy,
        eligibility_status=EligibilityStatus.DATA_INCOMPLETE,
        capital_feasibility=False,
        rejection_reasons=[RejectionReason.DATA_INCOMPLETE],
        gate_results=[],
        model_version=MODEL_VERSION,
        config_version=config.config_version,
        evaluated_at=now,
        ranking_explanation=f"Evaluation failed: {detail}",
    )
