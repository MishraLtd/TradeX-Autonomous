"""
Feature engineering. FEATURE_VERSION must bump on any change to the
functions below, since historical predictions are versioned against it
(Section 54).

Convention: every feature column is computed with pandas rolling/ewm
windows that are right-closed at the current row (never centered, never
using .shift(-k)). This is what makes them causal by construction. The
leakage test in tests/test_leakage.py checks this empirically by mutating
future rows and asserting past feature values are unaffected.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

FEATURE_VERSION = "risk_features_v1"

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def compute_atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.rolling(window, min_periods=window).mean()


def compute_rolling_vol(df: pd.DataFrame, window: int) -> pd.Series:
    r = _log_returns(df["close"])
    return r.rolling(window, min_periods=window).std() * np.sqrt(252)  # annualized


def compute_ewma_vol(df: pd.DataFrame, span: int = 20) -> pd.Series:
    r = _log_returns(df["close"])
    return r.ewm(span=span, min_periods=span).std() * np.sqrt(252)


def compute_sma_distance(df: pd.DataFrame, window: int) -> pd.Series:
    sma = df["close"].rolling(window, min_periods=window).mean()
    return (df["close"] - sma) / sma * 100.0


def compute_rsi(df: pd.DataFrame, window: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window, min_periods=window).mean()
    avg_loss = loss.rolling(window, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_relative_volume(df: pd.DataFrame, window: int = 20) -> pd.Series:
    avg_vol = df["volume"].rolling(window, min_periods=window).mean()
    return df["volume"] / avg_vol.replace(0, np.nan)


def compute_gap_pct(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return (df["open"] - prev_close) / prev_close * 100.0


def compute_range_pct(df: pd.DataFrame) -> pd.Series:
    return (df["high"] - df["low"]) / df["close"] * 100.0


def compute_drawdown_from_high(df: pd.DataFrame, window: int = 60) -> pd.Series:
    rolling_high = df["high"].rolling(window, min_periods=window).max()
    return (df["close"] - rolling_high) / rolling_high * 100.0


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """df must be a single symbol, sorted ascending by timestamp, with
    REQUIRED_COLUMNS present. Returns a new frame aligned index-for-index
    with df (same length), all NaN-padded at the start where windows
    aren't yet full — those rows must be dropped before training/inference,
    never filled with a placeholder (Section 32: fail safe on missing data)."""
    missing = set(REQUIRED_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns for feature build: {missing}")

    out = pd.DataFrame(index=df.index)
    out["timestamp"] = df["timestamp"]

    out["log_return_1d"] = _log_returns(df["close"])
    out["atr_14"] = compute_atr(df, 14)
    out["atr_14_pct"] = out["atr_14"] / df["close"] * 100.0
    out["vol_5d"] = compute_rolling_vol(df, 5)
    out["vol_10d"] = compute_rolling_vol(df, 10)
    out["vol_20d"] = compute_rolling_vol(df, 20)
    out["ewma_vol_20"] = compute_ewma_vol(df, 20)
    out["dist_sma20_pct"] = compute_sma_distance(df, 20)
    out["dist_sma50_pct"] = compute_sma_distance(df, 50)
    out["dist_sma200_pct"] = compute_sma_distance(df, 200)
    out["rsi_14"] = compute_rsi(df, 14)
    out["relative_volume_20"] = compute_relative_volume(df, 20)
    out["gap_pct"] = compute_gap_pct(df)
    out["range_pct"] = compute_range_pct(df)
    out["drawdown_from_high_60"] = compute_drawdown_from_high(df, 60)

    # trade-configuration features are appended later at the trade level
    # (stop_distance_pct, target_distance_pct, holding_horizon) since they
    # depend on the specific candidate trade, not the raw series.
    return out


FEATURE_COLUMNS = [
    "log_return_1d", "atr_14_pct", "vol_5d", "vol_10d", "vol_20d",
    "ewma_vol_20", "dist_sma20_pct", "dist_sma50_pct", "dist_sma200_pct",
    "rsi_14", "relative_volume_20", "gap_pct", "range_pct",
    "drawdown_from_high_60",
]
