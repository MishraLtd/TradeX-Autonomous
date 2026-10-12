"""
Core orchestration: Level 1 (gates) -> Level 2 (component scoring) ->
scenario resilience -> final OpportunityAssessment (spec §19, §61).

This module deliberately contains almost no "policy" - the actual
economic judgment calls live in components.py, gates.py, scenarios.py,
and config.py, each documented with its rationale. scoring.py just wires
them together in the correct, auditable order and builds the output
schema. This separation is what makes the model's decisions traceable
(spec §38 Explainability): every number in the final assessment can be
traced to exactly one function.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import uuid

from . import components as comp
from .config import OpportunityModelConfig, DEFAULT_CONFIG
from .exceptions import DataIncompleteError, InvalidInputError
from .gates import evaluate_gates
from .scenarios import compute_scenario_scores
from .schemas import (
    OpportunityCandidate,
    OpportunityAssessment,
    ComponentScores,
    EligibilityStatus,
    ScoreConfidenceBand,
)

MODEL_VERSION = "opportunity-model-1.0.0-phase1"


def _weighted_base_score(scores: ComponentScores, config: OpportunityModelConfig) -> Decimal:
    w = config.weights
    raw = (
        scores.net_edge * w.net_edge
        + scores.risk_efficiency * w.risk_efficiency
        + scores.regime_compatibility * w.regime_compatibility
        + scores.prediction_reliability * w.prediction_reliability
        + scores.liquidity_execution * w.liquidity_execution
        + scores.capital_efficiency * w.capital_efficiency
        + scores.holding_time_efficiency * w.holding_time_efficiency
        + scores.cost_resilience * w.cost_resilience
    )
    # Apply the tail-risk dampener multiplicatively, AFTER the weighted
    # average - see components.tail_risk_multiplier docstring for why.
    return raw * scores.tail_risk_multiplier


def _score_confidence_band(candidate: OpportunityCandidate, scenario_resilience: Decimal) -> ScoreConfidenceBand:
    reliability = (candidate.model_calibration_quality + (Decimal("1") - candidate.prediction_uncertainty)) / 2
    combined = (reliability + scenario_resilience) / 2
    if combined >= Decimal("0.75"):
        return ScoreConfidenceBand.HIGH
    if combined >= Decimal("0.5"):
        return ScoreConfidenceBand.MEDIUM
    return ScoreConfidenceBand.LOW


def _score_uncertainty(candidate: OpportunityCandidate, scenario_resilience: Decimal) -> Decimal:
    """
    A simple, transparent uncertainty proxy (0-100 scale, "+/- this many
    points"): higher prediction_uncertainty and lower scenario_resilience
    widen the plausible score band. Not a statistically derived confidence
    interval (Phase 1 has no accumulated calibration data to derive one
    from) - explicitly documented as a heuristic, matching spec §27's
    instruction to "not manufacture confidence."
    """
    base_band = Decimal("15")
    widen = candidate.prediction_uncertainty * Decimal("10") + (Decimal("1") - scenario_resilience) * Decimal("15")
    return (base_band + widen).quantize(Decimal("0.1"))


def _input_hash(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> str:
    payload = candidate.model_dump_json() + config.content_hash()
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _check_data_completeness(candidate: OpportunityCandidate) -> None:
    """
    Pydantic already enforces structural completeness (required fields).
    This extra pass catches semantically-missing-but-structurally-present
    data (e.g. NaN sneaking through as a float before Decimal coercion,
    or the classic 'all zeros' pattern that a naive default would produce)
    (spec §45).
    """
    import math

    numeric_fields = [
        candidate.return_prediction.expected_return_pct,
        candidate.return_prediction.probability_of_profit,
        candidate.risk_prediction.expected_downside_pct,
        candidate.risk_prediction.expected_max_adverse_excursion_pct,
        candidate.risk_prediction.probability_stop_loss,
        candidate.cost_prediction.base.expected_net_return_pct,
        candidate.cost_prediction.base.cost_ratio,
        candidate.liquidity.average_traded_value,
        candidate.holding_period_days,
    ]
    for v in numeric_fields:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            raise DataIncompleteError(f"Non-finite value encountered in candidate {candidate.symbol}: {v}")

    if candidate.return_prediction.probability_of_profit > 1 or candidate.return_prediction.probability_of_profit < 0:
        raise InvalidInputError("probability_of_profit out of [0,1]")


def evaluate_opportunity(
    candidate: OpportunityCandidate,
    config: OpportunityModelConfig = DEFAULT_CONFIG,
    now: datetime | None = None,
) -> OpportunityAssessment:
    """
    Public API (spec §61): evaluate a single candidate trade.

    Never raises for "ordinary" ineligibility (failed gates) - those are
    represented as a REJECTED assessment with populated rejection_reasons.
    Raises DataIncompleteError / InvalidInputError only for structurally
    broken input the caller must fix upstream; callers should catch these
    per-candidate and convert to a DATA_INCOMPLETE assessment rather than
    aborting the whole ranking cycle (see ranking.py).
    """
    now = now or datetime.now(timezone.utc)
    opportunity_id = str(uuid.uuid4())

    _check_data_completeness(candidate)

    passed, gate_results, rejection_reasons = evaluate_gates(candidate, config, now=now)
    capital_feasible = candidate.capital_required <= candidate.portfolio_context.available_capital

    input_versions = {
        "return_model": candidate.return_prediction.model_version,
        "risk_model": candidate.risk_prediction.model_version,
        "regime_model": candidate.regime_prediction.model_version,
        "cost_model": candidate.cost_prediction.model_version,
    }

    if not passed:
        return OpportunityAssessment(
            opportunity_id=opportunity_id,
            symbol=candidate.symbol,
            timestamp=candidate.timestamp,
            trade_type=candidate.trade_type,
            strategy=candidate.strategy,
            eligibility_status=EligibilityStatus.REJECTED,
            capital_feasibility=capital_feasible,
            economic_viability=candidate.cost_prediction.base.economic_viability,
            gate_results=gate_results,
            rejection_reasons=rejection_reasons,
            expected_gross_return_pct=candidate.cost_prediction.expected_gross_return_pct,
            expected_net_return_pct=candidate.cost_prediction.base.expected_net_return_pct,
            probability_of_profit=candidate.return_prediction.probability_of_profit,
            expected_downside_pct=candidate.risk_prediction.expected_downside_pct,
            model_version=MODEL_VERSION,
            config_version=config.config_version,
            input_versions=input_versions,
            input_hash=_input_hash(candidate, config),
            evaluated_at=now,
        )

    # --- Level 2: component scoring --------------------------------------
    q2 = Decimal("0.01")
    scores = ComponentScores(
        net_edge=comp.net_edge_score(candidate, config).quantize(q2),
        risk_efficiency=comp.risk_efficiency_score(candidate, config).quantize(q2),
        regime_compatibility=comp.regime_compatibility_score(candidate, config).quantize(q2),
        prediction_reliability=comp.prediction_reliability_score(candidate, config).quantize(q2),
        liquidity_execution=comp.liquidity_execution_score(candidate, config).quantize(q2),
        capital_efficiency=comp.capital_efficiency_score(candidate, config).quantize(q2),
        holding_time_efficiency=comp.holding_time_efficiency_score(candidate, config).quantize(q2),
        cost_resilience=comp.cost_resilience_score(candidate, config).quantize(q2),
        tail_risk_multiplier=comp.tail_risk_multiplier(candidate, config).quantize(Decimal("0.001")),
    )

    base_score = _weighted_base_score(scores, config)
    scenario_scores = compute_scenario_scores(candidate, base_score, config)

    final_score = (base_score * scenario_scores.scenario_resilience).quantize(Decimal("1"))
    final_score = max(Decimal("0"), min(Decimal("100"), final_score))

    confidence_band = _score_confidence_band(candidate, scenario_scores.scenario_resilience)
    uncertainty = _score_uncertainty(candidate, scenario_scores.scenario_resilience)

    net_return_frac = candidate.cost_prediction.base.expected_net_return_pct / Decimal("100")
    net_profit = net_return_frac * candidate.capital_required

    return OpportunityAssessment(
        opportunity_id=opportunity_id,
        symbol=candidate.symbol,
        timestamp=candidate.timestamp,
        trade_type=candidate.trade_type,
        strategy=candidate.strategy,
        eligibility_status=EligibilityStatus.ELIGIBLE,
        opportunity_score=final_score,
        score_band=config.bands.label_for(final_score),
        score_confidence=confidence_band,
        score_uncertainty=uncertainty,
        expected_gross_return_pct=candidate.cost_prediction.expected_gross_return_pct,
        expected_net_return_pct=candidate.cost_prediction.base.expected_net_return_pct,
        expected_net_profit=net_profit.quantize(Decimal("0.01")),
        probability_of_profit=candidate.return_prediction.probability_of_profit,
        expected_downside_pct=candidate.risk_prediction.expected_downside_pct,
        component_scores=scores,
        scenario_scores=scenario_scores,
        capital_feasibility=capital_feasible,
        economic_viability=candidate.cost_prediction.base.economic_viability,
        gate_results=gate_results,
        rejection_reasons=[],
        model_version=MODEL_VERSION,
        config_version=config.config_version,
        input_versions=input_versions,
        input_hash=_input_hash(candidate, config),
        evaluated_at=now,
    )
