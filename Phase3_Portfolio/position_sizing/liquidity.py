from decimal import Decimal, ROUND_DOWN
from typing import Optional

from .config import SizingConfig
from .models import LiquiditySizingConstraint


def calculate_liquidity_adjustment(
    entry_price: Decimal,
    average_traded_value: Optional[Decimal],
    config: SizingConfig,
    external_constraint: Optional[LiquiditySizingConstraint] = None,
) -> LiquiditySizingConstraint:
    """Returns a LiquiditySizingConstraint with a share-count cap. Liquidity is
    treated as a hard CAP, not a multiplier — see DESIGN.md Part 10."""
    if external_constraint is not None and external_constraint.liquidity_cap_quantity is not None:
        return external_constraint

    if average_traded_value is None or average_traded_value <= 0 or entry_price <= 0:
        return LiquiditySizingConstraint(
            max_participation_value=None,
            liquidity_cap_quantity=None,
            note="liquidity data unavailable — no liquidity cap applied",
        )

    max_participation_value = average_traded_value * config.max_participation_pct
    cap_qty = int((max_participation_value / entry_price).to_integral_value(rounding=ROUND_DOWN))

    return LiquiditySizingConstraint(
        max_participation_value=max_participation_value,
        liquidity_cap_quantity=cap_qty,
        note=f"capped at {config.max_participation_pct * 100}% of average traded value",
    )
