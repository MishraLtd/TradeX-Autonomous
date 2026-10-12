from decimal import Decimal
from typing import Optional

from .enums import BindingConstraint
from .models import (
    CapitalFeasibilityConstraint,
    LiquiditySizingConstraint,
    PortfolioManagerInstruction,
)


def apply_constraints(
    size_after_adjustments: Decimal,
    entry_price: Decimal,
    capital_available_for_sizing: Decimal,
    request_max_quantity: Optional[int],
    request_min_quantity: Optional[int],
    request_max_position_value: Optional[Decimal],
    liquidity_constraint: LiquiditySizingConstraint,
    capital_constraint: Optional[CapitalFeasibilityConstraint],
    portfolio_instruction: Optional[PortfolioManagerInstruction],
) -> dict:
    """Applies the fixed precedence of caps (never multipliers) from
    DESIGN.md Part 15. Returns the capped raw quantity plus which cap bound
    (the smallest one) — the binding constraint."""
    candidates: list[tuple[Decimal, BindingConstraint]] = []

    candidates.append((size_after_adjustments, BindingConstraint.RISK_BUDGET))

    # capital cap (own capital_available_for_sizing, always applies as a floor-level sanity cap)
    if entry_price > 0:
        own_capital_cap_qty = capital_available_for_sizing / entry_price
        candidates.append((own_capital_cap_qty, BindingConstraint.CAPITAL_CAP))

    # Capital Feasibility interface cap
    if capital_constraint is not None and capital_constraint.max_affordable_quantity is not None:
        candidates.append(
            (Decimal(capital_constraint.max_affordable_quantity), BindingConstraint.CAPITAL_CAP)
        )
    if capital_constraint is not None and capital_constraint.max_affordable_value is not None and entry_price > 0:
        candidates.append(
            (capital_constraint.max_affordable_value / entry_price, BindingConstraint.CAPITAL_CAP)
        )

    # liquidity cap
    if liquidity_constraint.liquidity_cap_quantity is not None:
        candidates.append(
            (Decimal(liquidity_constraint.liquidity_cap_quantity), BindingConstraint.LIQUIDITY_CAP)
        )

    # portfolio manager / request max position value cap
    if request_max_position_value is not None and entry_price > 0:
        candidates.append((request_max_position_value / entry_price, BindingConstraint.PORTFOLIO_CAP))
    if (
        portfolio_instruction is not None
        and portfolio_instruction.preferred_exposure is not None
        and entry_price > 0
    ):
        candidates.append(
            (portfolio_instruction.preferred_exposure / entry_price, BindingConstraint.PORTFOLIO_CAP)
        )

    # hard config max quantity
    if request_max_quantity is not None:
        candidates.append((Decimal(request_max_quantity), BindingConstraint.MAX_QUANTITY_CONFIG))

    min_candidate_value, binding = min(candidates, key=lambda c: c[0])

    return {
        "capped_qty_raw": max(Decimal("0"), min_candidate_value),
        "binding_constraint": binding,
        "all_candidates": candidates,
    }
