from decimal import Decimal
from typing import Optional, Tuple

from .config import SizingConfig
from .exceptions import InvalidRequestError, MissingCriticalInputError


def compute_risk_per_share(entry_price: Decimal, stop_loss: Optional[Decimal]) -> Decimal:
    if stop_loss is None:
        raise MissingCriticalInputError("stop_loss is required for risk-based sizing")
    gross_risk = entry_price - stop_loss
    if gross_risk <= 0:
        raise InvalidRequestError(
            f"Non-positive risk per share (entry={entry_price}, stop={stop_loss}); "
            "stop must be below entry for a long position."
        )
    return gross_risk


def compute_net_risk_per_share(
    gross_risk_per_share: Decimal, expected_total_cost: Optional[Decimal]
) -> Decimal:
    cost_buffer = expected_total_cost if expected_total_cost is not None else Decimal("0")
    if cost_buffer < 0:
        cost_buffer = Decimal("0")
    return gross_risk_per_share + cost_buffer


def compute_risk_budget(
    capital_available_for_sizing: Decimal,
    max_risk_allowed: Optional[Decimal],
    config: SizingConfig,
) -> Decimal:
    if capital_available_for_sizing <= 0:
        raise InvalidRequestError("capital_available_for_sizing must be positive")

    hard_cap = capital_available_for_sizing * config.hard_max_risk_fraction
    if max_risk_allowed is not None:
        budget = min(max_risk_allowed, hard_cap)
    else:
        budget = min(
            capital_available_for_sizing * config.default_risk_fraction, hard_cap
        )
    return budget


def calculate_risk_based_size(
    entry_price: Decimal,
    stop_loss: Optional[Decimal],
    expected_total_cost: Optional[Decimal],
    capital_available_for_sizing: Decimal,
    max_risk_allowed: Optional[Decimal],
    config: SizingConfig,
) -> Tuple[Decimal, dict]:
    """Returns (base_risk_size_as_decimal_shares, diagnostics_dict)."""
    if entry_price is None or entry_price <= 0:
        raise InvalidRequestError(f"entry_price must be positive, got {entry_price}")

    gross_risk_per_share = compute_risk_per_share(entry_price, stop_loss)
    net_risk_per_share = compute_net_risk_per_share(gross_risk_per_share, expected_total_cost)
    risk_budget = compute_risk_budget(capital_available_for_sizing, max_risk_allowed, config)

    base_size = risk_budget / net_risk_per_share

    diagnostics = {
        "gross_risk_per_share": gross_risk_per_share,
        "net_risk_per_share": net_risk_per_share,
        "risk_pct_of_entry": gross_risk_per_share / entry_price,
        "risk_budget": risk_budget,
        "base_risk_size_raw": base_size,
    }
    return base_size, diagnostics
