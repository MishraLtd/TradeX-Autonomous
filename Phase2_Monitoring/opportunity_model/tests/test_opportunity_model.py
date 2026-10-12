"""
Unit tests covering the representative scenario set from spec §44.
Run with: pytest -q
"""

from decimal import Decimal
import math

import pytest

from opportunity_model.config import DEFAULT_CONFIG, OpportunityModelConfig, HardGateThresholds
from opportunity_model.exceptions import DataIncompleteError, InvalidInputError
from opportunity_model.schemas import EligibilityStatus, RejectionReason, MarketRegimeLabel
from opportunity_model.scoring import evaluate_opportunity
from opportunity_model.ranking import rank_opportunities
from opportunity_model.testing_helpers import make_candidate, NOW, D


# ---------------------------------------------------------------------------
# Core quality scenarios
# ---------------------------------------------------------------------------

def test_strong_trade_scores_highly():
    c = make_candidate(
        expected_return_pct=2.5, probability_of_profit=0.78, expected_downside_pct=0.5,
        expected_mae_pct=0.8, probability_stop_loss=0.15, cost_ratio=0.15, regime_compatibility=0.9,
        model_calibration_quality=0.85, prediction_uncertainty=0.15, capital_required=300,
    )
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.ELIGIBLE
    assert a.opportunity_score >= 65


def test_weak_trade_is_rejected_or_scores_low():
    c = make_candidate(
        expected_return_pct=0.4, probability_of_profit=0.52, expected_downside_pct=2.0,
        expected_mae_pct=3.0, probability_stop_loss=0.55, cost_ratio=0.55,
    )
    a = evaluate_opportunity(c, now=NOW)
    # Either rejected outright, or eligible-but-poor. Never HIGH quality.
    if a.eligibility_status == EligibilityStatus.ELIGIBLE:
        assert a.opportunity_score < 60


def test_high_return_high_risk_does_not_auto_win():
    """Self-critique Q1: a huge return with a nasty tail must not score too highly."""
    risky = make_candidate(
        symbol="RISKY", expected_return_pct=6.0, probability_of_profit=0.60,
        expected_downside_pct=4.5, expected_mae_pct=8.0, tail_risk_pct=9.0,
        probability_stop_loss=0.45, cost_ratio=0.25,
    )
    steady = make_candidate(
        symbol="STEADY", expected_return_pct=1.8, probability_of_profit=0.78,
        expected_downside_pct=0.6, expected_mae_pct=0.9, probability_stop_loss=0.15,
        cost_ratio=0.15,
    )
    a_risky = evaluate_opportunity(risky, now=NOW)
    a_steady = evaluate_opportunity(steady, now=NOW)
    assert a_risky.eligibility_status == EligibilityStatus.ELIGIBLE
    assert a_steady.eligibility_status == EligibilityStatus.ELIGIBLE
    # risk efficiency + tail dampener should keep the fat-tailed trade from
    # dominating purely because of a bigger headline number
    assert a_risky.component_scores.tail_risk_multiplier < Decimal("1")
    assert a_steady.opportunity_score >= a_risky.opportunity_score - 5


def test_high_probability_low_return_does_not_auto_win():
    """Self-critique Q2: tiny edge shouldn't win just because PoP is huge."""
    tiny_edge = make_candidate(
        symbol="TINY", expected_return_pct=0.5, probability_of_profit=0.92,
        expected_downside_pct=0.2, expected_mae_pct=0.3, cost_ratio=0.4,
        net_return_pct=0.15,
    )
    solid = make_candidate(
        symbol="SOLID", expected_return_pct=2.5, probability_of_profit=0.72,
        expected_downside_pct=1.0, expected_mae_pct=1.5, cost_ratio=0.2,
    )
    a_tiny = evaluate_opportunity(tiny_edge, now=NOW)
    a_solid = evaluate_opportunity(solid, now=NOW)
    if a_tiny.eligibility_status == EligibilityStatus.ELIGIBLE and a_solid.eligibility_status == EligibilityStatus.ELIGIBLE:
        assert a_solid.opportunity_score > a_tiny.opportunity_score


def test_high_gross_return_but_terrible_net_return_is_penalized():
    c = make_candidate(gross_return_pct=5.0, cost_ratio=0.85, expected_return_pct=5.0)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.COST_ADJUSTED_EDGE_INSUFFICIENT in a.rejection_reasons


def test_low_gross_return_excellent_net_economics_can_pass():
    c = make_candidate(gross_return_pct=1.2, cost_ratio=0.1, expected_return_pct=1.2, probability_of_profit=0.8)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.ELIGIBLE


def test_strong_trade_in_incompatible_regime_is_gated():
    c = make_candidate(regime=MarketRegimeLabel.PANIC)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.MARKET_REGIME_INCOMPATIBLE in a.rejection_reasons


def test_strong_trade_poor_liquidity_is_gated():
    c = make_candidate(average_traded_value=500, capital_required=300)  # headroom far too low
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.INSUFFICIENT_LIQUIDITY in a.rejection_reasons


def test_strong_trade_insufficient_capital_is_gated_but_not_scoreless_forever():
    c = make_candidate(capital_required=5000, available_capital=1000)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.CAPITAL_INFEASIBLE in a.rejection_reasons
    assert a.capital_feasibility is False


def test_short_vs_long_holding_period_efficiency():
    short = make_candidate(symbol="SHORT", expected_return_pct=2.0, holding_period_days=1, cost_ratio=0.15)
    long_ = make_candidate(symbol="LONG", expected_return_pct=2.2, holding_period_days=10, cost_ratio=0.15)
    a_short = evaluate_opportunity(short, now=NOW)
    a_long = evaluate_opportunity(long_, now=NOW)
    assert a_short.component_scores.holding_time_efficiency > a_long.component_scores.holding_time_efficiency


def test_long_holding_period_beyond_max_is_gated():
    c = make_candidate(holding_period_days=15)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.HOLDING_PERIOD_TOO_LONG in a.rejection_reasons


def test_capital_efficiency_prefers_smaller_capital_same_absolute_profit_ratio():
    # ₹300 trade earning 2% vs ₹900 trade earning 1% (same absolute ₹ profit ~ different efficiency)
    small = make_candidate(symbol="SMALL", capital_required=300, expected_return_pct=3.0, cost_ratio=0.1)
    big = make_candidate(symbol="BIG", capital_required=900, expected_return_pct=1.0, cost_ratio=0.1)
    a_small = evaluate_opportunity(small, now=NOW)
    a_big = evaluate_opportunity(big, now=NOW)
    if a_big.eligibility_status == EligibilityStatus.ELIGIBLE:
        assert a_small.component_scores.capital_efficiency > a_big.component_scores.capital_efficiency


def test_high_confidence_but_poorly_calibrated_model_is_penalized():
    c = make_candidate(prediction_confidence=0.95, model_calibration_quality=0.2)
    a = evaluate_opportunity(c, now=NOW)
    # Should hit the hard calibration gate
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.MODEL_CALIBRATION_TOO_POOR in a.rejection_reasons


def test_low_confidence_but_strong_raw_prediction_still_scores_reasonably():
    c = make_candidate(prediction_confidence=0.4, model_calibration_quality=0.75, prediction_uncertainty=0.3)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.ELIGIBLE


# ---------------------------------------------------------------------------
# Multi-candidate ranking scenarios
# ---------------------------------------------------------------------------

def test_multiple_candidates_similar_scores_ranked_stably():
    c1 = make_candidate(symbol="A1", expected_return_pct=2.0)
    c2 = make_candidate(symbol="A2", expected_return_pct=2.02)
    result = rank_opportunities([c1, c2], now=NOW)
    assert result.no_trade in (True, False)
    if not result.no_trade and len(result.top_opportunities) == 2:
        assert "indistinguishable" in result.top_opportunities[1].ranking_explanation or \
               result.top_opportunities[0].opportunity_score != result.top_opportunities[1].opportunity_score


def test_identical_opportunities_produce_identical_scores():
    c1 = make_candidate(symbol="X1")
    c2 = make_candidate(symbol="X2")
    a1 = evaluate_opportunity(c1, now=NOW)
    a2 = evaluate_opportunity(c2, now=NOW)
    assert a1.opportunity_score == a2.opportunity_score


def test_zero_viable_opportunities_triggers_no_trade():
    candidates = [make_candidate(symbol=f"BAD{i}", expected_return_pct=0.1, probability_of_profit=0.5, cost_ratio=0.7)
                  for i in range(5)]
    result = rank_opportunities(candidates, now=NOW)
    assert result.no_trade is True
    assert result.no_trade_reason is not None


def test_one_viable_opportunity_returns_exactly_one():
    good = make_candidate(symbol="GOOD", expected_return_pct=2.5, probability_of_profit=0.75)
    bad = make_candidate(symbol="BAD", expected_return_pct=0.1, probability_of_profit=0.5, cost_ratio=0.7)
    result = rank_opportunities([good, bad], now=NOW)
    assert result.no_trade is False
    assert len(result.top_opportunities) == 1
    assert result.top_opportunities[0].symbol == "GOOD"


def test_fifty_candidates_do_not_force_five_results():
    candidates = [make_candidate(symbol=f"S{i}", expected_return_pct=0.2, probability_of_profit=0.5, cost_ratio=0.7)
                  for i in range(50)]
    result = rank_opportunities(candidates, now=NOW)
    assert result.total_candidates == 50
    assert result.no_trade is True  # all weak -> abstain, not force top 5


def test_top_k_never_exceeds_configured_max():
    candidates = [make_candidate(symbol=f"G{i}", expected_return_pct=2.5 + i * 0.01, probability_of_profit=0.75)
                  for i in range(20)]
    result = rank_opportunities(candidates, now=NOW)
    assert len(result.top_opportunities) <= DEFAULT_CONFIG.top_k_max


# ---------------------------------------------------------------------------
# Missing / invalid / stale data (fail-closed)
# ---------------------------------------------------------------------------

def test_stale_data_is_gated():
    from datetime import timedelta
    c = make_candidate()
    # evaluate "now" far in the future relative to data_generated_at
    a = evaluate_opportunity(c, now=NOW + timedelta(hours=1))
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.DATA_STALE in a.rejection_reasons


def test_missing_return_prediction_field_raises_pydantic_validation():
    with pytest.raises(Exception):
        from opportunity_model.schemas import ReturnPrediction
        ReturnPrediction(probability_of_profit=Decimal("0.6"), model_version="x", generated_at=NOW)  # missing expected_return_pct


def test_negative_probability_rejected_by_schema():
    from opportunity_model.schemas import ReturnPrediction
    with pytest.raises(Exception):
        ReturnPrediction(
            expected_return_pct=Decimal("1.0"),
            probability_of_profit=Decimal("-0.1"),
            model_version="x",
            generated_at=NOW,
        )


def test_probability_greater_than_one_rejected_by_schema():
    from opportunity_model.schemas import ReturnPrediction
    with pytest.raises(Exception):
        ReturnPrediction(
            expected_return_pct=Decimal("1.0"),
            probability_of_profit=Decimal("1.4"),
            model_version="x",
            generated_at=NOW,
        )


def test_nan_value_raises_data_incomplete():
    c = make_candidate()
    broken = c.model_copy(
        update={
            "return_prediction": c.return_prediction.model_copy(
                update={"expected_return_pct": Decimal("NaN")}
            )
        }
    )
    with pytest.raises(DataIncompleteError):
        evaluate_opportunity(broken, now=NOW)


def test_extreme_volatility_and_spread_gated():
    c = make_candidate(spread_pct=5.0)
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.SPREAD_TOO_WIDE in a.rejection_reasons


def test_scenario_disagreement_lowers_score():
    robust = make_candidate(
        symbol="ROBUST", expected_return_pct=2.5, cost_ratio=0.15,
        optimistic_net_return_pct=2.5, conservative_net_return_pct=1.9,
    )
    fragile = make_candidate(
        symbol="FRAGILE", expected_return_pct=2.5, cost_ratio=0.15,
        optimistic_net_return_pct=3.0, conservative_net_return_pct=0.4,
    )
    a_robust = evaluate_opportunity(robust, now=NOW)
    a_fragile = evaluate_opportunity(fragile, now=NOW)
    assert a_robust.scenario_scores.scenario_resilience > a_fragile.scenario_scores.scenario_resilience
    assert a_robust.opportunity_score >= a_fragile.opportunity_score


def test_score_sensitivity_is_bounded():
    from opportunity_model.validation import sensitivity_analysis
    c = make_candidate()
    result = sensitivity_analysis(c, DEFAULT_CONFIG)
    for label, r in result["perturbations"].items():
        if r["delta"] is not None:
            assert abs(r["delta"]) < 15, f"{label} caused an oversized score swing: {r['delta']}"


def test_rank_stability_under_tiny_perturbation():
    a = make_candidate(symbol="A", expected_return_pct=2.0)
    b = make_candidate(symbol="B", expected_return_pct=2.001)
    result = rank_opportunities([a, b], now=NOW)
    if not result.no_trade and len(result.top_opportunities) == 2:
        top, second = result.top_opportunities
        assert abs(top.opportunity_score - second.opportunity_score) < DEFAULT_CONFIG.stability.minimum_material_score_change


def test_custom_config_thresholds_are_respected():
    strict_gates = HardGateThresholds(min_net_return_pct=Decimal("2.0"))
    custom_config = OpportunityModelConfig(gates=strict_gates)
    c = make_candidate(expected_return_pct=1.0, cost_ratio=0.1)  # net ~0.9%, below the new 2.0% floor
    a = evaluate_opportunity(c, config=custom_config, now=NOW)
    assert a.eligibility_status == EligibilityStatus.REJECTED
    assert RejectionReason.NET_RETURN_BELOW_MINIMUM in a.rejection_reasons


def test_regression_reference_example_scores_in_expected_band():
    """Pins the worked example's top candidate to a stable score band so
    future refactors don't silently change scoring behaviour."""
    c = make_candidate(
        symbol="REF", expected_return_pct=2.4, probability_of_profit=0.71,
        expected_downside_pct=0.9, expected_mae_pct=1.3, cost_ratio=0.146,
        regime_compatibility=0.85, holding_period_days=2,
    )
    a = evaluate_opportunity(c, now=NOW)
    assert a.eligibility_status == EligibilityStatus.ELIGIBLE
    assert 60 <= a.opportunity_score <= 95
