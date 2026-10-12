from decimal import Decimal
from typing import Optional

from .config import SizingConfig


def calculate_kelly_size(
    probability_of_profit: Optional[Decimal],
    probability_of_loss: Optional[Decimal],
    expected_upside: Optional[Decimal],
    expected_downside: Optional[Decimal],
    capital_available_for_sizing: Decimal,
    entry_price: Decimal,
    config: SizingConfig,
) -> Optional[dict]:
    """Fractional Kelly, hard-capped at config.kelly_hard_cap of capital,
    regardless of the raw formula's output. Comparison/diagnostic use only —
    never the live sizing method. See DESIGN.md Part 8."""
    if None in (probability_of_profit, probability_of_loss, expected_upside, expected_downside):
        return None
    if expected_downside <= 0 or entry_price <= 0:
        return None

    payoff_ratio = expected_upside / expected_downside
    if payoff_ratio == 0:
        return None

    raw_kelly = probability_of_profit - (probability_of_loss / payoff_ratio)
    fractional_kelly = raw_kelly * config.kelly_fraction

    capped_fraction = max(Decimal("0"), min(fractional_kelly, config.kelly_hard_cap))
    kelly_capital = capital_available_for_sizing * capped_fraction
    kelly_qty = int((kelly_capital / entry_price))

    return {
        "raw_kelly_fraction": raw_kelly,
        "fractional_kelly_fraction": fractional_kelly,
        "capped_kelly_fraction": capped_fraction,
        "kelly_candidate_qty": kelly_qty,
    }
