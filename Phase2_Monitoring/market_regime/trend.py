from __future__ import annotations
from typing import List, Optional

from .enums import TrendState
from .config import TrendConfig
from ._math_utils import sma, adx, linreg_slope


def compute_trend(
    closes: List[float], highs: List[float], lows: List[float], cfg: TrendConfig,
):
    """Returns (TrendState, signed_score in ~[-1,1], reason_codes: list[str]).

    Combines: MA structure (fast vs slow, price vs both), MA slope, and ADX
    (trend strength, direction-agnostic) - per §3A this must not collapse to
    a single indicator.
    """
    n = len(closes)
    reasons = []
    if n < cfg.sma_slow + cfg.slope_lookback:
        return TrendState.UNKNOWN, 0.0, ["INSUFFICIENT_HISTORY_TREND"]

    fast = sma(closes, cfg.sma_fast)
    slow = sma(closes, cfg.sma_slow)
    price = closes[-1]
    if fast is None or slow is None:
        return TrendState.UNKNOWN, 0.0, ["INSUFFICIENT_HISTORY_TREND"]

    ma_structure_score = 0.0
    if price > fast > slow:
        ma_structure_score = 1.0
        reasons.append("PRICE_ABOVE_FAST_ABOVE_SLOW")
    elif price < fast < slow:
        ma_structure_score = -1.0
        reasons.append("PRICE_BELOW_FAST_BELOW_SLOW")
    else:
        ma_structure_score = 0.0
        reasons.append("MA_STRUCTURE_MIXED")

    slow_series = [sma(closes, cfg.sma_slow, i) for i in range(n - cfg.slope_lookback, n)]
    if any(v is None for v in slow_series):
        slope = 0.0
    else:
        slope = linreg_slope(slow_series) or 0.0
    if abs(slope) < cfg.slope_flat_epsilon:
        reasons.append("SLOPE_FLAT")

    adx_value = adx(highs, lows, closes, period=14)
    adx_strength = 0.0
    if adx_value is not None:
        if adx_value >= cfg.adx_strong_threshold:
            adx_strength = 1.0
            reasons.append(f"ADX_STRONG({adx_value:.1f})")
        elif adx_value <= cfg.adx_flat_threshold:
            adx_strength = 0.0
            reasons.append(f"ADX_WEAK({adx_value:.1f})")
        else:
            adx_strength = (adx_value - cfg.adx_flat_threshold) / (
                cfg.adx_strong_threshold - cfg.adx_flat_threshold
            )
    else:
        reasons.append("ADX_UNAVAILABLE")

    direction = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
    combined = 0.5 * ma_structure_score + 0.5 * direction
    signed_score = combined * (0.4 + 0.6 * adx_strength)  # ADX gates strength, not direction
    signed_score = max(-1.0, min(1.0, signed_score))

    if adx_strength < 0.15 or abs(slope) < cfg.slope_flat_epsilon:
        state = TrendState.FLAT
    elif signed_score >= 0.6:
        state = TrendState.STRONG_UP
    elif signed_score >= 0.15:
        state = TrendState.WEAK_UP
    elif signed_score <= -0.6:
        state = TrendState.STRONG_DOWN
    elif signed_score <= -0.15:
        state = TrendState.WEAK_DOWN
    else:
        state = TrendState.FLAT

    return state, signed_score, reasons
