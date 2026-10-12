from decimal import Decimal
from typing import Optional

from .models import PositionSizingRequest, PortfolioRiskSizingConstraint


def validate_request(
    request: PositionSizingRequest,
    portfolio_risk_constraint: Optional[PortfolioRiskSizingConstraint] = None,
) -> Optional[str]:
    """Returns None if valid, else a human-readable rejection reason string.
    Fail-closed: any structural problem here means SIZING_INCOMPLETE /
    NO_POSITION, never a fabricated size."""
    if portfolio_risk_constraint is not None and portfolio_risk_constraint.halt_sizing:
        return "Portfolio Risk halt_sizing=True"

    if request.entry_price is None or request.entry_price <= 0:
        return f"entry_price must be positive, got {request.entry_price}"

    if request.capital_available_for_sizing is None or request.capital_available_for_sizing <= 0:
        return f"capital_available_for_sizing must be positive, got {request.capital_available_for_sizing}"

    if request.stop_loss is None and not request.allow_synthetic_stop_fallback:
        return "stop_loss missing and synthetic-stop fallback not explicitly enabled"

    if request.stop_loss is not None and request.stop_loss >= request.entry_price:
        return (
            f"stop_loss ({request.stop_loss}) must be below entry_price "
            f"({request.entry_price}) for a long position"
        )

    if request.max_quantity is not None and request.max_quantity < 0:
        return "max_quantity cannot be negative"

    if request.min_quantity is not None and request.min_quantity < 0:
        return "min_quantity cannot be negative"

    return None
