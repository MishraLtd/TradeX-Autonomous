"""
Normalization utilities (spec §19).

All normalization is min-max with explicit clipping to a *documented*
range (no learned/backtest-derived ranges — that would risk future-data
leakage in backtests, spec §19/§37). Ranges live in config.py.
"""

from typing import Tuple


def clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def min_max_normalize(value: float, value_range: Tuple[float, float]) -> float:
    """Normalize to [0, 1] via clipped min-max. Robust: values outside the
    documented range are clipped rather than extrapolated, so a single
    outlier candidate cannot distort every other candidate's scale
    (a property pure z-score normalization over the batch would lack)."""
    lo, hi = value_range
    if hi <= lo:
        return 0.0
    v = clip(value, lo, hi)
    return (v - lo) / (hi - lo)
