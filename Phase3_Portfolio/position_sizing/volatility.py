from decimal import Decimal
from typing import Optional

from .config import SizingConfig


def clamp(value: Decimal, lo: Decimal, hi: Decimal) -> Decimal:
    return max(lo, min(hi, value))


def calculate_volatility_adjustment(
    atr_pct: Optional[Decimal], config: SizingConfig, vol_mult_floor_override: Optional[Decimal] = None
) -> Decimal:
    """Bounded, non-exploding volatility multiplier. Returns 1.0 (neutral) if
    atr_pct is unavailable — the sizing LEVEL is what records this degradation,
    not a fabricated volatility estimate here."""
    if atr_pct is None or atr_pct <= 0:
        return Decimal("1.00")

    ratio = config.reference_atr_pct / atr_pct
    lo = vol_mult_floor_override if vol_mult_floor_override is not None else config.min_vol_mult
    return clamp(ratio, lo, config.max_vol_mult)
