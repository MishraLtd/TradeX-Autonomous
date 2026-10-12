"""
Single source of truth for quantity rounding. Never inline rounding logic
elsewhere in the package (Part 14 / Part 23 discipline).
"""
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Optional


def round_quantity(
    raw_qty: Decimal,
    min_quantity: Optional[int] = None,
    max_quantity: Optional[int] = None,
    mode: str = "DOWN",
) -> int:
    """Round a raw (possibly fractional) share quantity to an integer.

    Default mode DOWN: never rounds a quantity upward past a value that could
    exceed a risk or capital limit. `mode="NEAREST"` is available for
    non-live/diagnostic use (e.g. frontier display) only.
    """
    if raw_qty is None or raw_qty < 0:
        return 0

    if mode == "NEAREST":
        q = int(raw_qty.to_integral_value(rounding=ROUND_HALF_UP))
    else:
        q = int(raw_qty.to_integral_value(rounding=ROUND_DOWN))

    if max_quantity is not None:
        q = min(q, max_quantity)
    if min_quantity is not None and q < min_quantity:
        # Never round UP past a hard min just to satisfy it — caller decides
        # whether sub-minimum quantity means NO_POSITION (Part 13/24).
        pass

    return max(q, 0)
