from decimal import Decimal
from typing import Optional


def calculate_expected_value_size(
    probability_of_profit: Optional[Decimal],
    probability_of_loss: Optional[Decimal],
    expected_upside: Optional[Decimal],
    expected_downside: Optional[Decimal],
) -> Optional[Decimal]:
    """EV per share, for diagnostics/economic-viability checks only — never
    used directly to set live quantity (DESIGN.md Part 7)."""
    if None in (probability_of_profit, probability_of_loss, expected_upside, expected_downside):
        return None
    return (probability_of_profit * expected_upside) - (probability_of_loss * expected_downside)
