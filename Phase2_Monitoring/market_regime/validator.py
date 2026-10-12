from __future__ import annotations
import time
from typing import List

from .schemas import InstrumentSeries
from .config import DataFreshnessConfig
from .exceptions import InsufficientDataError, StaleDataError


def check_sufficiency(series: InstrumentSeries, cfg: DataFreshnessConfig) -> None:
    min_bars = cfg.min_bars_required.get(series.timeframe.value, 60)
    if len(series.bars) < min_bars:
        raise InsufficientDataError(
            f"{series.symbol}/{series.timeframe.value}: have {len(series.bars)} bars, "
            f"need >= {min_bars}"
        )


def check_freshness(series: InstrumentSeries, cfg: DataFreshnessConfig, now: float = None) -> None:
    now = now if now is not None else time.time()
    if not series.bars:
        raise StaleDataError(f"{series.symbol}/{series.timeframe.value}: no bars at all")
    max_stale = cfg.max_staleness_seconds.get(series.timeframe.value, 3600)
    last_ts = series.bars[-1].timestamp
    age = now - last_ts
    if age > max_stale:
        raise StaleDataError(
            f"{series.symbol}/{series.timeframe.value}: last bar is {age:.0f}s old, "
            f"max allowed {max_stale}s"
        )


def bars_are_monotonic(series: InstrumentSeries) -> bool:
    ts = [b.timestamp for b in series.bars]
    return all(ts[i] < ts[i + 1] for i in range(len(ts) - 1))


def sanity_check_ohlc(series: InstrumentSeries) -> List[str]:
    """Non-fatal sanity flags (added to reason_codes rather than raised),
    e.g. high < low, negative volume/price - signals of upstream corruption
    that should push confidence down / trigger closer scrutiny without
    necessarily aborting the whole pipeline over a single bad print."""
    issues = []
    for b in series.bars[-5:]:
        if b.high < b.low:
            issues.append("OHLC_HIGH_LT_LOW")
        if b.close <= 0 or b.open <= 0:
            issues.append("OHLC_NONPOSITIVE_PRICE")
        if b.volume < 0:
            issues.append("OHLC_NEGATIVE_VOLUME")
    return list(set(issues))
