"""
PART — POSITION SIZING BRIDGE

Position Sizing answers "how much should we ideally allocate"; Capital
Feasibility answers "how much can we actually afford" (spec §27). This
module:

1. Builds a TradeCapitalRequest from the Position Sizing REQUEST (for
   trade identity/price) and RESULT (for the requested quantity) —
   Capital Feasibility never re-derives a size, it evaluates the one
   Position Sizing already produced.
2. Converts a CapitalFeasibilityResult back into the
   `position_sizing.models.CapitalFeasibilityConstraint` shape that
   Position Sizing's `constraints.apply_constraints()` already knows how
   to consume, so a second Position Sizing pass (if the caller re-runs
   one) can fold the verified affordability figure in as one more cap.
"""
from __future__ import annotations

from decimal import Decimal

from ..models import CapitalFeasibilityResult, LiquidityContext, TradeCapitalRequest


def trade_request_from_position_sizing(
    sizing_request,  # position_sizing.models.PositionSizingRequest
    sizing_result,   # position_sizing.models.PositionSizeResult
    sector: str = None,
    lot_size: int = 1,
) -> TradeCapitalRequest:
    if sizing_result.recommended_quantity <= 0:
        # Position Sizing already said NO_POSITION / SIZING_INCOMPLETE —
        # Capital Feasibility has nothing to evaluate. Callers should
        # generally short-circuit before reaching here; this is a safe,
        # explicit zero-quantity request rather than a crash.
        requested_qty = 0
    else:
        requested_qty = sizing_result.recommended_quantity

    return TradeCapitalRequest(
        position_id=sizing_request.position_id,
        opportunity_id=sizing_request.opportunity_id,
        symbol=sizing_request.symbol,
        exchange=sizing_request.exchange,
        sector=sector,
        trade_type=sizing_request.trade_type.value,
        side="BUY",
        entry_price=sizing_request.entry_price,
        requested_quantity=requested_qty,
        lot_size=lot_size,
        liquidity=LiquidityContext(
            average_daily_traded_value=sizing_request.average_traded_value,
            average_daily_volume=(
                int(sizing_request.average_volume)
                if sizing_request.average_volume is not None
                else None
            ),
        ),
        model_versions=dict(sizing_request.model_versions),
    )


def to_position_sizing_constraint(result: CapitalFeasibilityResult):
    """Returns a `position_sizing.models.CapitalFeasibilityConstraint`-
    shaped object (built lazily by the caller with the real class, since
    this package does not hard-depend on position_sizing) as a plain
    dict of the four fields that class defines, in the same order/names,
    so `CapitalFeasibilityConstraint(**this_dict)` works directly."""
    note = result.explanation
    return {
        "max_affordable_value": result.max_affordable_value,
        "max_affordable_quantity": result.max_affordable_quantity,
        "available_capital": result.available_capital,
        "capital_constraint_note": note,
    }
