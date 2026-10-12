"""
Validation utilities (spec §33, §34, §51, §52, §53, §26).

These functions operate on already-realized outcomes (i.e. they are used
AFTER trades have closed and the actual net return is known) and on
already-generated OpportunityAssessments. They never touch live
candidates. This module has no opinion on whether the model is "good" -
it just computes the metrics spec §34 lists so a human/AI Research
Council reviewer can judge that out-of-sample, on locked test data.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import List, Sequence, Tuple
import copy

from .config import OpportunityModelConfig
from .schemas import OpportunityAssessment, OpportunityCandidate
from .scoring import evaluate_opportunity


@dataclass
class RealizedOutcome:
    opportunity_id: str
    realized_net_return_pct: Decimal
    realized_drawdown_pct: Decimal
    realized_holding_period_days: Decimal
    hit: bool  # realized_net_return_pct > 0


# ---------------------------------------------------------------------------
# Ranking / top-K metrics (spec §34, §53)
# ---------------------------------------------------------------------------

def top_k_realized_return(
    assessments: List[OpportunityAssessment],
    outcomes: dict,
    k: int,
) -> dict:
    """
    outcomes: opportunity_id -> RealizedOutcome
    Returns average/median realized net return, hit rate, and average
    drawdown for the top-K ranked assessments (by absolute_rank).
    """
    ranked = sorted(
        (a for a in assessments if a.absolute_rank is not None),
        key=lambda a: a.absolute_rank,
    )[:k]
    matched = [outcomes[a.opportunity_id] for a in ranked if a.opportunity_id in outcomes]
    if not matched:
        return {"n": 0}

    returns = sorted(o.realized_net_return_pct for o in matched)
    n = len(returns)
    hit_rate = sum(1 for o in matched if o.hit) / n
    avg_drawdown = sum(o.realized_drawdown_pct for o in matched) / n

    return {
        "n": n,
        "avg_realized_net_return_pct": sum(returns) / n,
        "median_realized_net_return_pct": returns[n // 2],
        "hit_rate": Decimal(str(hit_rate)),
        "avg_realized_drawdown_pct": avg_drawdown,
    }


def score_bucket_analysis(
    assessments: List[OpportunityAssessment],
    outcomes: dict,
    buckets: Sequence[Tuple[int, int]] = ((0, 20), (20, 40), (40, 60), (60, 70), (70, 80), (80, 90), (90, 101)),
) -> dict:
    """spec §52: does realized outcome quality increase monotonically with score bucket?"""
    result = {}
    for low, high in buckets:
        bucket_assessments = [
            a for a in assessments
            if a.opportunity_score is not None and low <= a.opportunity_score < high
        ]
        matched = [outcomes[a.opportunity_id] for a in bucket_assessments if a.opportunity_id in outcomes]
        if not matched:
            result[f"{low}-{high}"] = {"n": 0}
            continue
        n = len(matched)
        result[f"{low}-{high}"] = {
            "n": n,
            "avg_realized_net_return_pct": sum(o.realized_net_return_pct for o in matched) / n,
            "hit_rate": Decimal(str(sum(1 for o in matched if o.hit) / n)),
        }
    return result


def spearman_rank_correlation(scores: List[Decimal], realized_returns: List[Decimal]) -> Decimal:
    """
    Simple Spearman correlation without external dependencies, for
    ranking-quality evaluation (spec §34). Ties are handled via average
    rank (standard approach).
    """
    n = len(scores)
    if n < 2 or n != len(realized_returns):
        raise ValueError("scores and realized_returns must be equal-length and length>=2")

    def rank(values: List[Decimal]) -> List[Decimal]:
        indexed = sorted(range(n), key=lambda i: values[i])
        ranks = [Decimal(0)] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and values[indexed[j + 1]] == values[indexed[i]]:
                j += 1
            avg_rank = Decimal(sum(range(i + 1, j + 2))) / Decimal(j - i + 1)
            for m in range(i, j + 1):
                ranks[indexed[m]] = avg_rank
            i = j + 1
        return ranks

    rx = rank(scores)
    ry = rank(realized_returns)
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return Decimal(1) - (Decimal(6) * d2) / (Decimal(n) * (Decimal(n) ** 2 - 1))


# ---------------------------------------------------------------------------
# Sensitivity / stability analysis (spec §26)
# ---------------------------------------------------------------------------

def sensitivity_analysis(
    candidate: OpportunityCandidate,
    config: OpportunityModelConfig,
    fields_to_perturb: List[str] | None = None,
) -> dict:
    """
    Perturbs a set of numeric leaf fields on the candidate by
    +/- config.stability.perturbation_test_fraction and reports the
    resulting opportunity_score delta for each. A model where a small
    input perturbation causes a huge score swing fails this check and
    should not be trusted for live ranking (spec §26).

    Uses candidate.model_copy(update=...) style perturbation on the
    handful of fields the spec identifies as most score-sensitive; this
    is a targeted stability check, not exhaustive fuzzing.
    """
    baseline = evaluate_opportunity(candidate, config=config)
    if baseline.opportunity_score is None:
        return {"baseline_ineligible": True}

    frac = config.stability.perturbation_test_fraction
    results = {}

    def perturb_return(mult: Decimal):
        new_rp = candidate.return_prediction.model_copy(
            update={"expected_return_pct": candidate.return_prediction.expected_return_pct * mult}
        )
        new_cost_base = candidate.cost_prediction.base.model_copy(
            update={"expected_net_return_pct": candidate.cost_prediction.base.expected_net_return_pct * mult}
        )
        new_cost = candidate.cost_prediction.model_copy(update={"base": new_cost_base})
        return candidate.model_copy(update={"return_prediction": new_rp, "cost_prediction": new_cost})

    for label, mult in (("return_up_5pct", Decimal("1") + frac), ("return_down_5pct", Decimal("1") - frac)):
        perturbed = perturb_return(mult)
        result = evaluate_opportunity(perturbed, config=config)
        delta = (result.opportunity_score - baseline.opportunity_score) if result.opportunity_score is not None else None
        results[label] = {"score": result.opportunity_score, "delta": delta}

    return {"baseline_score": baseline.opportunity_score, "perturbations": results}
