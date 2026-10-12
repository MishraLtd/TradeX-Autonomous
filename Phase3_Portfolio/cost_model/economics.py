"""
PART 10 — EXPECTED RETURN THRESHOLD
PART 15 — LOW-CAPITAL TRADE GATE
"""
from __future__ import annotations

from decimal import Decimal
from typing import List, Tuple

from .config import GateConfig, DEFAULT_GATE_CONFIG
from .models import EconomicViability


def minimum_required_gross_return_pct(
    friction_pct: Decimal,
    safety_buffer_pct: Decimal = None,
    gate: GateConfig = DEFAULT_GATE_CONFIG,
) -> Decimal:
    """
    PART 10 — minimum_required_gross_return()

    required_gross_return = total_friction_pct + safety_buffer_pct + min_net_return_pct

    i.e. the gross move needed to (a) pay for every cost, (b) leave an
    explicit slack for estimation error, and (c) still clear the gate's
    minimum acceptable net return.
    """
    buffer = safety_buffer_pct if safety_buffer_pct is not None else gate.min_required_safety_buffer_pct
    return (friction_pct + buffer + gate.min_net_return_pct).quantize(Decimal("0.0001"))


def evaluate_viability(
    net_pnl: Decimal,
    net_return_pct: Decimal,
    total_cost: Decimal,
    gross_pnl: Decimal,
    break_even_move_pct: Decimal,
    slippage_tier: str,
    market_impact_tier: str,
    position_value: Decimal,
    gate: GateConfig = DEFAULT_GATE_CONFIG,
) -> Tuple[EconomicViability, List[str]]:
    """
    PART 15 — hard economic gate. Returns (PASS|REJECT, reasons[]).
    ALL applicable failing checks are reported, not just the first one,
    so a caller (or a human debugging a rejected trade) can see the
    full picture in one pass.
    """
    reasons: List[str] = []

    if net_pnl <= gate.min_net_profit_rupees:
        reasons.append(
            f"expected_net_profit ₹{net_pnl} <= minimum_profit_threshold ₹{gate.min_net_profit_rupees}"
        )

    if net_return_pct <= gate.min_net_return_pct:
        reasons.append(
            f"expected_net_return {net_return_pct}% <= minimum_required_net_return {gate.min_net_return_pct}%"
        )

    if gross_pnl > 0:
        cost_ratio = (total_cost / gross_pnl)
        if cost_ratio > gate.max_cost_ratio:
            reasons.append(
                f"cost_ratio {cost_ratio*100:.1f}% of gross profit > maximum_allowed_cost_ratio "
                f"{gate.max_cost_ratio*100:.1f}%"
            )
    else:
        reasons.append("gross_pnl <= 0 — cost ratio undefined / trade has no positive edge to cost against")

    if break_even_move_pct > gate.max_break_even_move_pct:
        reasons.append(
            f"break_even_move {break_even_move_pct}% > expected_realistic_move "
            f"{gate.max_break_even_move_pct}%"
        )

    if position_value < gate.min_viable_position_value:
        reasons.append(
            f"position_value ₹{position_value} < min_viable_position_value "
            f"₹{gate.min_viable_position_value} — fixed per-order costs likely dominate"
        )

    if gate.reject_on_high_uncertainty_tier:
        if slippage_tier in gate.high_uncertainty_tiers or market_impact_tier in gate.high_uncertainty_tiers:
            reasons.append(
                f"execution-cost uncertainty too high: slippage_tier={slippage_tier}, "
                f"market_impact_tier={market_impact_tier} used a blind fallback rather than real market data"
            )

    verdict = EconomicViability.REJECT if reasons else EconomicViability.PASS
    return verdict, reasons
