"""
Level 1 — Hard Eligibility Gates (spec §18, §19).

Answers: "Can this trade be considered AT ALL?" PASS / REJECT only.
No scoring happens here. A candidate that fails any gate never receives
an opportunity_score - it is not "scored low", it is out of consideration,
which is a structurally different statement (spec §19).

Gates are independent and ALL are evaluated (not short-circuited) so that
a rejected candidate's explanation can list every reason it failed, not
just the first one encountered - this materially helps debugging /
explainability (spec §39, §63).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Tuple

from .config import OpportunityModelConfig
from .schemas import OpportunityCandidate, GateResult, RejectionReason


def _age_seconds(ts: datetime, now: datetime) -> Decimal:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return Decimal(str((now - ts).total_seconds()))


def evaluate_gates(
    candidate: OpportunityCandidate,
    config: OpportunityModelConfig,
    now: datetime | None = None,
) -> Tuple[bool, List[GateResult], List[RejectionReason]]:
    """
    Returns (all_passed, gate_results, rejection_reasons).
    """
    now = now or datetime.now(timezone.utc)
    g = config.gates
    results: List[GateResult] = []
    reasons: List[RejectionReason] = []

    def check(name: str, passed: bool, detail: str, reason: RejectionReason | None):
        results.append(GateResult(gate_name=name, passed=passed, detail=detail))
        if not passed and reason is not None:
            reasons.append(reason)

    # --- Staleness -----------------------------------------------------
    age = _age_seconds(candidate.data_generated_at, now)
    check(
        "data_freshness",
        age <= Decimal(g.max_data_age_seconds),
        f"input age={age}s, max={g.max_data_age_seconds}s",
        RejectionReason.DATA_STALE,
    )

    # --- Economic viability (Cost Model authoritative) ------------------
    base_cost = candidate.cost_prediction.base
    check(
        "economic_viability",
        base_cost.economic_viability,
        f"Cost Model economic_viability={base_cost.economic_viability}",
        RejectionReason.ECONOMIC_VIABILITY_FAILED,
    )

    check(
        "min_net_return",
        base_cost.expected_net_return_pct >= g.min_net_return_pct,
        f"net_return={base_cost.expected_net_return_pct}% < min={g.min_net_return_pct}%",
        RejectionReason.NET_RETURN_BELOW_MINIMUM,
    )

    check(
        "max_cost_ratio",
        base_cost.cost_ratio <= g.max_cost_ratio,
        f"cost_ratio={base_cost.cost_ratio} > max={g.max_cost_ratio}",
        RejectionReason.COST_ADJUSTED_EDGE_INSUFFICIENT,
    )

    expected_move = abs(candidate.return_prediction.expected_return_pct)
    if expected_move > 0:
        be_ratio = base_cost.break_even_move_pct / expected_move
    else:
        be_ratio = Decimal("999")
    check(
        "break_even_feasibility",
        be_ratio <= g.max_break_even_move_vs_expected_move_ratio,
        f"break_even/{expected_move}% expected move ratio={be_ratio:.2f} "
        f"> max={g.max_break_even_move_vs_expected_move_ratio}",
        RejectionReason.BREAK_EVEN_MOVE_TOO_DEMANDING,
    )

    # --- Probability & risk ---------------------------------------------
    check(
        "min_probability_of_profit",
        candidate.return_prediction.probability_of_profit >= g.min_probability_of_profit,
        f"PoP={candidate.return_prediction.probability_of_profit} < min={g.min_probability_of_profit}",
        RejectionReason.PROBABILITY_OF_PROFIT_TOO_LOW,
    )

    check(
        "max_expected_downside",
        candidate.risk_prediction.expected_downside_pct <= g.max_expected_downside_pct,
        f"downside={candidate.risk_prediction.expected_downside_pct}% > max={g.max_expected_downside_pct}%",
        RejectionReason.RISK_LIMIT_EXCEEDED,
    )

    check(
        "max_probability_stop_loss",
        candidate.risk_prediction.probability_stop_loss <= g.max_probability_of_stop_loss,
        f"P(stop)={candidate.risk_prediction.probability_stop_loss} > max={g.max_probability_of_stop_loss}",
        RejectionReason.STOP_LOSS_PROBABILITY_TOO_HIGH,
    )

    # --- Liquidity --------------------------------------------------------
    headroom = candidate.liquidity.average_traded_value / candidate.capital_required
    check(
        "liquidity_headroom",
        headroom >= g.min_relative_liquidity_headroom,
        f"avg_traded_value/capital_required={headroom:.1f}x < min={g.min_relative_liquidity_headroom}x",
        RejectionReason.INSUFFICIENT_LIQUIDITY,
    )

    check(
        "relative_order_size",
        candidate.liquidity.relative_order_size <= g.max_relative_order_size,
        f"relative_order_size={candidate.liquidity.relative_order_size} > max={g.max_relative_order_size}",
        RejectionReason.RELATIVE_ORDER_SIZE_TOO_LARGE,
    )

    check(
        "spread_width",
        candidate.liquidity.spread_pct <= g.max_spread_pct,
        f"spread={candidate.liquidity.spread_pct}% > max={g.max_spread_pct}%",
        RejectionReason.SPREAD_TOO_WIDE,
    )

    # --- Model reliability -------------------------------------------------
    check(
        "model_calibration_floor",
        candidate.model_calibration_quality >= g.min_model_calibration_quality,
        f"calibration_quality={candidate.model_calibration_quality} < min={g.min_model_calibration_quality}",
        RejectionReason.MODEL_CALIBRATION_TOO_POOR,
    )

    check(
        "prediction_uncertainty_ceiling",
        candidate.prediction_uncertainty <= g.max_prediction_uncertainty,
        f"uncertainty={candidate.prediction_uncertainty} > max={g.max_prediction_uncertainty}",
        RejectionReason.PREDICTION_UNCERTAINTY_TOO_HIGH,
    )

    # --- Regime -------------------------------------------------------------
    regime_prohibited = candidate.regime_prediction.regime.value in g.prohibited_regimes_by_default
    check(
        "regime_not_prohibited",
        not regime_prohibited,
        f"regime={candidate.regime_prediction.regime.value} is in prohibited set "
        f"{g.prohibited_regimes_by_default}",
        RejectionReason.MARKET_REGIME_INCOMPATIBLE,
    )

    # --- Holding period -------------------------------------------------------
    check(
        "max_holding_period",
        candidate.holding_period_days <= g.max_holding_period_days,
        f"holding_period={candidate.holding_period_days}d > max={g.max_holding_period_days}d",
        RejectionReason.HOLDING_PERIOD_TOO_LONG,
    )

    # --- Capital feasibility (separate concept from score, spec §17) ---------
    capital_feasible = candidate.capital_required <= candidate.portfolio_context.available_capital
    check(
        "capital_feasibility",
        capital_feasible,
        f"capital_required={candidate.capital_required} > available={candidate.portfolio_context.available_capital}",
        RejectionReason.CAPITAL_INFEASIBLE,
    )

    all_passed = all(r.passed for r in results)
    return all_passed, results, reasons
