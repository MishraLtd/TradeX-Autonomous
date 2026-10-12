import random
import time
from market_regime.schemas import Bar, InstrumentSeries
from market_regime.enums import Timeframe


def make_series(symbol, closes, timeframe=Timeframe.D1, start_ts=None, step=86400,
                 vol_base=1_000_000, high_low_spread=0.01):
    """By default the series ENDS at (roughly) `now`, so freshness checks in
    validator.py pass without every test having to compute/pass a matching
    `now` - only tests that specifically exercise staleness override this."""
    if start_ts is None:
        start_ts = time.time() - (len(closes) - 1) * step
    bars = []
    prev_close = closes[0]
    for i, c in enumerate(closes):
        high = max(c, prev_close) * (1 + high_low_spread)
        low = min(c, prev_close) * (1 - high_low_spread)
        bars.append(Bar(
            timestamp=start_ts + i * step,
            open=prev_close,
            high=high,
            low=low,
            close=c,
            volume=vol_base,
        ))
        prev_close = c
    return InstrumentSeries(symbol=symbol, timeframe=timeframe, bars=tuple(bars))


def trending_up_closes(n=300, start=100.0, daily_drift=0.003, noise=0.004, seed=1):
    rnd = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        drift = daily_drift + rnd.uniform(-noise, noise)
        closes.append(closes[-1] * (1 + drift))
    return closes


def trending_down_closes(n=300, start=100.0, daily_drift=-0.003, noise=0.004, seed=2):
    rnd = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        drift = daily_drift + rnd.uniform(-noise, noise)
        closes.append(closes[-1] * (1 + drift))
    return closes


def sideways_closes(n=300, start=100.0, noise=0.004, seed=3):
    rnd = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        drift = rnd.uniform(-noise, noise)
        nxt = closes[-1] * (1 + drift)
        # mean-revert gently toward `start` so it doesn't wander into a trend
        nxt += (start - nxt) * 0.02
        closes.append(nxt)
    return closes


def low_vol_closes(n=300, start=100.0, noise=0.0008, seed=4):
    rnd = random.Random(seed)
    closes = [start]
    for _ in range(n - 1):
        drift = rnd.uniform(-noise, noise)
        closes.append(closes[-1] * (1 + drift))
    return closes


def panic_closes(n=300, start=100.0, seed=5):
    """Calm history, then a violent multi-day crash at the tail."""
    rnd = random.Random(seed)
    closes = low_vol_closes(n - 10, start=start, seed=seed)
    for _ in range(10):
        drop = rnd.uniform(0.05, 0.09)
        closes.append(closes[-1] * (1 - drop))
    return closes
