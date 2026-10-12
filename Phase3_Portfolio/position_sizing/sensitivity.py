from decimal import Decimal
from typing import List, Optional

from .config import SizingConfig
from .models import PositionSizingRequest
from .risk_based import calculate_risk_based_size


def stop_loss_sensitivity(
    request: PositionSizingRequest, stop_offsets: List[Decimal], config: SizingConfig
) -> List[dict]:
    """Recompute base risk size across a small grid of alternative stop
    distances, holding everything else fixed (Part 31)."""
    results = []
    for offset in stop_offsets:
        candidate_stop = request.entry_price - offset
        try:
            size, diag = calculate_risk_based_size(
                request.entry_price,
                candidate_stop,
                request.expected_total_cost,
                request.capital_available_for_sizing,
                request.max_risk_allowed,
                config,
            )
            results.append({"stop": candidate_stop, "risk_per_share": offset, "base_size_raw": size})
        except Exception as exc:  # noqa: BLE001 — diagnostic table, not live path
            results.append({"stop": candidate_stop, "risk_per_share": offset, "error": str(exc)})
    return results


def position_size_frontier(
    request: PositionSizingRequest,
    max_qty_to_scan: int,
    config: SizingConfig,
) -> List[dict]:
    """Produces expected gross/net profit and risk for q = 1..max_qty_to_scan
    (Part 65). Diagnostic/reporting only — the live engine does not select
    from this table directly."""
    frontier = []
    if request.stop_loss is None:
        return frontier

    risk_per_share = request.entry_price - request.stop_loss
    cost_per_share = request.expected_total_cost or Decimal("0")
    net_return_per_share: Optional[Decimal] = None
    if request.expected_upside is not None and request.probability_of_profit is not None:
        net_return_per_share = request.expected_upside - cost_per_share

    for q in range(1, max_qty_to_scan + 1):
        qd = Decimal(q)
        position_value = qd * request.entry_price
        estimated_risk = qd * risk_per_share
        expected_gross_profit = (qd * request.expected_upside) if request.expected_upside is not None else None
        expected_net_profit = (
            (qd * net_return_per_share) if net_return_per_share is not None else None
        )
        frontier.append(
            {
                "quantity": q,
                "position_value": position_value,
                "estimated_risk": estimated_risk,
                "expected_gross_profit": expected_gross_profit,
                "expected_net_profit": expected_net_profit,
                "capital_requirement": position_value,
            }
        )
    return frontier
