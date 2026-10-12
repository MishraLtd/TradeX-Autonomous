from decimal import Decimal
from typing import Optional

from .config import SizingConfig


def calculate_confidence_adjustment(
    model_confidence: Optional[Decimal], config: SizingConfig
) -> Decimal:
    """Piecewise-linear, bounded confidence multiplier — see DESIGN.md Part 6.
    Returns 1.0 (neutral) if confidence is unavailable."""
    if model_confidence is None:
        return Decimal("1.00")

    c = max(Decimal("0"), min(Decimal("1"), model_confidence))

    if c < config.confidence_ramp_low:
        return config.min_confidence_mult

    if c < config.confidence_ramp_high:
        span = config.confidence_ramp_high - config.confidence_ramp_low
        progress = (c - config.confidence_ramp_low) / span
        return config.min_confidence_mult + progress * (Decimal("1.00") - config.min_confidence_mult)

    # confidence_ramp_high .. 1.00 -> 1.00 .. max_confidence_mult
    span = Decimal("1.00") - config.confidence_ramp_high
    if span == 0:
        return config.max_confidence_mult
    progress = (c - config.confidence_ramp_high) / span
    return Decimal("1.00") + progress * (config.max_confidence_mult - Decimal("1.00"))
