"""
PART 12 — PORTFOLIO MANAGER INTERFACE

Exposes capital-efficiency metrics so the Portfolio Manager never
allocates purely on predicted-return rank.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .models import CostBreakdown


@dataclass
class CapitalEfficiency:
    expected_net_return_pct: Decimal
    expected_net_profit: Decimal
    total_expected_friction: Decimal
    capital_committed: Decimal
    net_profit_per_rupee_committed: Decimal          # expected_net_profit / capital_committed
    net_profit_per_rupee_risked: Optional[Decimal]    # expected_net_profit / expected_risk
    minimum_viable_position_value: Decimal
    trade_viability: str


def capital_efficiency(
    breakdown: CostBreakdown,
    expected_risk_rupees: Optional[Decimal] = None,
    min_viable_position_value: Decimal = Decimal("300"),
) -> CapitalEfficiency:
    """
    expected_risk_rupees: caller-supplied worst-case rupee loss for this
    position (e.g. from the Risk Engine's stop-loss distance) — kept as
    an external input rather than guessed here, since risk sizing is the
    Risk Engine's responsibility, not the Cost Model's.
    """
    capital_committed = breakdown.trade.turnover
    net_profit_per_rupee = (
        (breakdown.net_pnl / capital_committed).quantize(Decimal("0.000001"))
        if capital_committed else Decimal("0")
    )
    net_profit_per_risk = (
        (breakdown.net_pnl / expected_risk_rupees).quantize(Decimal("0.000001"))
        if expected_risk_rupees and expected_risk_rupees > 0 else None
    )

    return CapitalEfficiency(
        expected_net_return_pct=breakdown.net_return_pct,
        expected_net_profit=breakdown.net_pnl,
        total_expected_friction=breakdown.total_cost,
        capital_committed=capital_committed,
        net_profit_per_rupee_committed=net_profit_per_rupee,
        net_profit_per_rupee_risked=net_profit_per_risk,
        minimum_viable_position_value=min_viable_position_value,
        trade_viability=breakdown.economic_viability.value,
    )


def is_worth_allocating(breakdown: CostBreakdown) -> bool:
    """Simple boolean bridge for the Portfolio Manager's allocation loop.
    Highest predicted/net return is deliberately NOT assumed to be the
    best trade elsewhere in the caller (PART 12) — this function only
    answers the binary economic-viability question for ONE candidate."""
    from .models import EconomicViability
    return breakdown.economic_viability == EconomicViability.PASS
