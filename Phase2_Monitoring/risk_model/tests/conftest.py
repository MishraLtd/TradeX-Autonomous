import numpy as np
import pandas as pd
import pytest


def make_synthetic_ohlcv(n=400, seed=7, start_price=100.0):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2023-01-02", periods=n)
    rets = rng.normal(0.0003, 0.018, size=n)
    close = start_price * np.exp(np.cumsum(rets))
    open_ = close * (1 + rng.normal(0, 0.004, size=n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, size=n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, size=n)))
    volume = rng.integers(50_000, 500_000, size=n).astype(float)
    df = pd.DataFrame(
        {
            "timestamp": dates,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    return df


@pytest.fixture
def ohlcv():
    return make_synthetic_ohlcv()
