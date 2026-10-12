"""Dependency-free numeric helpers (no numpy). Every function here is
causal: given a list ordered oldest->newest, it only uses elements up to
`end_index` (inclusive, defaults to the last element), never future ones
(§24 look-ahead prevention)."""

from __future__ import annotations
from typing import List, Optional
from statistics import pstdev


def sma(values: List[float], period: int, end_index: Optional[int] = None) -> Optional[float]:
    if end_index is None:
        end_index = len(values) - 1
    start = end_index - period + 1
    if start < 0 or end_index >= len(values) or end_index < 0:
        return None
    window = values[start:end_index + 1]
    return sum(window) / period


def rolling_std(values: List[float], period: int, end_index: Optional[int] = None) -> Optional[float]:
    if end_index is None:
        end_index = len(values) - 1
    start = end_index - period + 1
    if start < 0 or end_index >= len(values):
        return None
    window = values[start:end_index + 1]
    if len(window) < 2:
        return None
    return pstdev(window)


def pct_change(values: List[float], lookback: int, end_index: Optional[int] = None) -> Optional[float]:
    if end_index is None:
        end_index = len(values) - 1
    start = end_index - lookback
    if start < 0 or end_index >= len(values):
        return None
    base = values[start]
    if base == 0:
        return None
    return (values[end_index] - base) / base


def true_range(high: float, low: float, prev_close: Optional[float]) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def average_true_range(
    highs: List[float], lows: List[float], closes: List[float],
    period: int, end_index: Optional[int] = None,
) -> Optional[float]:
    if end_index is None:
        end_index = len(closes) - 1
    start = end_index - period + 1
    if start < 1 or end_index >= len(closes):  # need one prior close
        return None
    trs = []
    for i in range(max(start, 1), end_index + 1):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period - 1:
        return None
    return sum(trs) / len(trs)


def percentile_rank(values: List[float], value: float) -> Optional[float]:
    """Rank of `value` within `values` as a fraction in [0, 1], using only
    the values passed in (caller must pass a strictly historical window,
    ending at or before the current bar, to avoid look-ahead)."""
    if not values:
        return None
    n = len(values)
    below_or_equal = sum(1 for v in values if v <= value)
    return below_or_equal / n


def linreg_slope(values: List[float]) -> Optional[float]:
    """Simple OLS slope of values against an evenly spaced x-axis
    (0..n-1), normalized by the mean of `values` so it's comparable across
    instruments/price levels."""
    n = len(values)
    if n < 2:
        return None
    xs = list(range(n))
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, values))
    den = sum((x - x_mean) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den
    if y_mean == 0:
        return None
    return slope / abs(y_mean)


def adx(highs: List[float], lows: List[float], closes: List[float],
        period: int, end_index: Optional[int] = None) -> Optional[float]:
    """Standard Wilder ADX, computed causally over [end_index-2*period, end_index]."""
    if end_index is None:
        end_index = len(closes) - 1
    needed_start = end_index - 2 * period
    if needed_start < 1:
        return None

    plus_dm, minus_dm, trs = [], [], []
    for i in range(needed_start, end_index + 1):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))

    def wilder_smooth(series: List[float], p: int) -> List[float]:
        if len(series) < p:
            return []
        smoothed = [sum(series[:p])]
        for v in series[p:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / p) + v)
        return smoothed

    tr_s = wilder_smooth(trs, period)
    pdm_s = wilder_smooth(plus_dm, period)
    mdm_s = wilder_smooth(minus_dm, period)
    if not tr_s or not pdm_s or not mdm_s:
        return None

    dis = []
    for tr_v, pdm_v, mdm_v in zip(tr_s, pdm_s, mdm_s):
        if tr_v == 0:
            dis.append((0.0, 0.0))
            continue
        dis.append((100 * pdm_v / tr_v, 100 * mdm_v / tr_v))

    dxs = []
    for plus_di, minus_di in dis:
        denom = plus_di + minus_di
        dxs.append(0.0 if denom == 0 else 100 * abs(plus_di - minus_di) / denom)

    if len(dxs) < period:
        return None
    return sum(dxs[-period:]) / period


def zscore(values: List[float], value: float) -> Optional[float]:
    if len(values) < 2:
        return None
    m = sum(values) / len(values)
    sd = pstdev(values)
    if sd == 0:
        return 0.0
    return (value - m) / sd
