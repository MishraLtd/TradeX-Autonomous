"""
Explainability (spec §38, §39) — turns an OpportunityAssessment into the
human-readable TRADEX OPPORTUNITY REPORT format from the spec, for either
an eligible (scored) or rejected candidate.
"""

from __future__ import annotations

from .schemas import OpportunityAssessment, EligibilityStatus


def explain_opportunity(assessment: OpportunityAssessment, total_candidates: int | None = None) -> str:
    if assessment.eligibility_status != EligibilityStatus.ELIGIBLE:
        return _explain_rejection(assessment)
    return _explain_eligible(assessment, total_candidates)


def _explain_eligible(assessment: OpportunityAssessment, total_candidates: int | None) -> str:
    cs = assessment.component_scores
    ss = assessment.scenario_scores
    rank_line = ""
    if assessment.relative_rank is not None:
        total = total_candidates if total_candidates is not None else "?"
        rank_line = f"Rank: #{assessment.relative_rank} of {total}\n"

    reasons = []
    if cs:
        if cs.net_edge >= 70:
            reasons.append("strong probability-adjusted net edge")
        if cs.risk_efficiency >= 70:
            reasons.append("favourable risk profile")
        if cs.regime_compatibility >= 70:
            reasons.append("high regime compatibility")
        if cs.cost_resilience >= 60:
            reasons.append("acceptable cost burden")
        if cs.prediction_reliability >= 60:
            reasons.append("sufficient prediction reliability")
    reasons = [r for r in reasons if r]
    reason_text = "\n+\n".join(reasons) if reasons else "acceptable overall profile"

    caveat = ""
    if cs and cs.capital_efficiency < 50:
        caveat = "\n\nBut:\n\nCapital requirement limits position size relative to potential profit."
    if ss and ss.scenario_resilience < Decimal_or(0.75):
        caveat += (
            "\n\nNote: score is meaningfully lower under the conservative cost/return scenario "
            f"(resilience multiplier {ss.scenario_resilience})."
        )

    return f"""TRADEX OPPORTUNITY REPORT

Symbol: {assessment.symbol}

Trade Type: {assessment.trade_type.value}

Strategy: {assessment.strategy}

Opportunity Score: {assessment.opportunity_score}/100 ({assessment.score_band})

{rank_line}Expected Net Return: {_signed(assessment.expected_net_return_pct)}%

Probability of Profit: {_pct(assessment.probability_of_profit)}

Expected Downside: -{assessment.expected_downside_pct}%

Regime Compatibility: {cs.regime_compatibility if cs else 'n/a'}/100

Cost Resilience: {cs.cost_resilience if cs else 'n/a'}/100

Capital Efficiency: {cs.capital_efficiency if cs else 'n/a'}/100

Prediction Reliability: {cs.prediction_reliability if cs else 'n/a'}/100

Scenario Resilience: {ss.scenario_resilience if ss else 'n/a'}

Score Confidence: {assessment.score_confidence.value if assessment.score_confidence else 'n/a'} (+/- {assessment.score_uncertainty} pts)

Decision:

{assessment.score_band}-QUALITY OPPORTUNITY

Reason:

{reason_text}{caveat}
"""


def _explain_rejection(assessment: OpportunityAssessment) -> str:
    reasons = "\n".join(f"- {r.value}" for r in assessment.rejection_reasons) or "- UNSPECIFIED"
    return f"""TRADEX OPPORTUNITY REPORT

Symbol: {assessment.symbol}

Opportunity Score: N/A

Status: {assessment.eligibility_status.value}

Reason(s):

{reasons}

{assessment.ranking_explanation or ''}
"""


def _signed(value) -> str:
    return f"+{value}" if value is not None and value >= 0 else f"{value}"


def _pct(value) -> str:
    if value is None:
        return "n/a"
    return f"{(value * 100):.0f}%"


def Decimal_or(x):
    from decimal import Decimal
    return Decimal(str(x))
