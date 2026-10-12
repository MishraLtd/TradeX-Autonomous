from __future__ import annotations
from typing import List, Optional

from .enums import VolatilityState
from .config import VolatilityConfig
from ._math_utils import average_true_range, percentile_rank


def compute_volatility(
    closes: List[float], highs: List[float], lows: List[float], cfg: VolatilityConfig,
):
    """Returns (VolatilityState, percentile[0,1] or None, reason_codes)."""
    n = len(closes)
    reasons = []
    min_needed = cfg.atr_period + cfg.percentile_lookback
    if n < min_needed:
        return VolatilityState.UNKNOWN, None, ["INSUFFICIENT_HISTORY_VOLATILITY"]

    atr_now = average_true_range(highs, lows, closes, cfg.atr_period)
    if atr_now is None:
        return VolatilityState.UNKNOWN, None, ["ATR_UNAVAILABLE"]

    # normalize ATR by price so it's comparable across the lookback window
    atr_norm_now = atr_now / closes[-1] if closes[-1] else None
    if atr_norm_now is None:
        return VolatilityState.UNKNOWN, None, ["ATR_NORMALIZATION_FAILED"]

    history = []
    for i in range(n - cfg.percentile_lookback, n - 1):  # strictly historical, excludes "now"
        atr_i = average_true_range(highs, lows, closes, cfg.atr_period, end_index=i)
        if atr_i is not None and closes[i]:
            history.append(atr_i / closes[i])

    if len(history) < cfg.percentile_lookback * 0.5:
        return VolatilityState.UNKNOWN, None, ["INSUFFICIENT_VOL_HISTORY_FOR_PERCENTILE"]

    pct = percentile_rank(history, atr_norm_now)
    if pct is None:
        return VolatilityState.UNKNOWN, None, ["PERCENTILE_UNAVAILABLE"]

    if pct >= cfg.extreme_pct:
        state = VolatilityState.EXTREME
        reasons.append(f"ATR_PCTL_EXTREME({pct:.2f})")
    elif pct >= cfg.high_pct:
        state = VolatilityState.HIGH
        reasons.append(f"ATR_PCTL_HIGH({pct:.2f})")
    elif pct <= cfg.low_pct:
        state = VolatilityState.LOW
        reasons.append(f"ATR_PCTL_LOW({pct:.2f})")
    else:
        state = VolatilityState.NORMAL
        reasons.append(f"ATR_PCTL_NORMAL({pct:.2f})")

    return state, pct, reasons
