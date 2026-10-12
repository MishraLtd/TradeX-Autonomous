"""
Feature Engine — Section 6.

HARD RULE enforced throughout this file: every feature at row `t` may
only use data from rows <= t (rolling windows, shifts with positive
lag, expanding windows). Any `.shift()` with a NEGATIVE argument is a
label, never a feature — grep for `shift(-` in this file; it should
return nothing.

Feature families implemented: price, trend, volatility, volume/liquidity,
momentum, market-context, time. Microstructure (bid/ask, depth) is left
as a documented stub since Kite historical depth data isn't reliably
available for backtesting (Section 6 microstructure note).
"""

from __future__ import annotations
import numpy as np
import pandas as pd


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.replace(0, np.nan)


class FeatureEngine:
    """Computes the full leakage-safe feature set for a single symbol's
    OHLCV history. Call `.transform(df, market_df)` per symbol.

    `df` columns required: open, high, low, close, volume (DatetimeIndex).
    `market_df` (optional): index-level OHLCV (e.g. NIFTY) aligned to the
    same calendar, used for market-context features.
    """

    def __init__(self, feature_version: str = "v1"):
        self.feature_version = feature_version

    # ---------------------------------------------------------------- price
    def _price_features(self, df: pd.DataFrame) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        close = df["close"]
        f["ret_1"] = close.pct_change(1)
        f["logret_1"] = np.log(close / close.shift(1))
        for h in (5, 10, 20, 60):
            f[f"mom_{h}"] = close.pct_change(h)
        f["accel_5_20"] = f["mom_5"] - f["mom_10"]
        f["dist_from_high_20"] = _safe_div(close, df["high"].rolling(20).max()) - 1
        f["dist_from_low_20"] = _safe_div(close, df["low"].rolling(20).min()) - 1
        f["breakout_dist_20"] = _safe_div(close - df["high"].rolling(20).max().shift(1), close)
        f["mean_reversion_dist_20"] = _safe_div(close - close.rolling(20).mean(), close.rolling(20).std())
        prev_close = close.shift(1)
        f["gap_pct"] = _safe_div(df["open"] - prev_close, prev_close)
        return f

    # ---------------------------------------------------------------- trend
    def _trend_features(self, df: pd.DataFrame) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        close = df["close"]
        sma20 = close.rolling(20).mean()
        sma50 = close.rolling(50).mean()
        sma200 = close.rolling(200, min_periods=100).mean()
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        f["dist_sma20"] = _safe_div(close, sma20) - 1
        f["dist_sma50"] = _safe_div(close, sma50) - 1
        f["dist_sma200"] = _safe_div(close, sma200) - 1
        f["sma20_slope"] = sma20.pct_change(5)
        f["sma50_slope"] = sma50.pct_change(10)
        f["trend_alignment"] = ((sma20 > sma50).astype(float) + (sma50 > sma200).astype(float)) - 1.0
        f["ema_spread"] = _safe_div(ema12 - ema26, close)
        return f

    # ----------------------------------------------------------- volatility
    def _volatility_features(self, df: pd.DataFrame) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        close, high, low = df["close"], df["high"], df["low"]
        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        f["atr_14"] = tr.rolling(14).mean()
        f["atr_pct_14"] = _safe_div(f["atr_14"], close)
        ret1 = close.pct_change(1)
        f["realized_vol_10"] = ret1.rolling(10).std() * np.sqrt(10)
        f["realized_vol_20"] = ret1.rolling(20).std() * np.sqrt(20)
        f["vol_expansion"] = _safe_div(f["realized_vol_10"], f["realized_vol_20"].replace(0, np.nan)) - 1
        f["range_pct"] = _safe_div(high - low, close)
        return f

    # -------------------------------------------------------- volume/liq.
    def _volume_features(self, df: pd.DataFrame) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        vol = df["volume"]
        f["vol_change_5"] = vol.pct_change(5)
        f["rel_volume_20"] = _safe_div(vol, vol.rolling(20).mean())
        f["traded_value"] = vol * df["close"]
        f["traded_value_pct_20"] = f["traded_value"].rolling(20).rank(pct=True)
        f["turnover_accel"] = _safe_div(vol.rolling(5).mean(), vol.rolling(20).mean()) - 1
        return f

    # ------------------------------------------------------------ momentum
    def _momentum_features(self, df: pd.DataFrame) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        close = df["close"]
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = _safe_div(gain, loss)
        f["rsi_14"] = 100 - (100 / (1 + rs))
        f["roc_10"] = close.pct_change(10)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        f["macd"] = macd
        f["macd_signal_diff"] = macd - signal
        low14 = df["low"].rolling(14).min()
        high14 = df["high"].rolling(14).max()
        f["stoch_k"] = _safe_div(close - low14, (high14 - low14).replace(0, np.nan)) * 100
        return f

    # -------------------------------------------------------------- market
    def _market_context_features(self, df: pd.DataFrame, market_df: pd.DataFrame | None) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        if market_df is None:
            return f
        m_close = market_df["close"].reindex(df.index).ffill()
        f["market_ret_1"] = m_close.pct_change(1)
        f["market_ret_5"] = m_close.pct_change(5)
        f["market_vol_20"] = m_close.pct_change(1).rolling(20).std()
        stock_ret5 = df["close"].pct_change(5)
        f["rel_strength_vs_market_5"] = stock_ret5 - f["market_ret_5"]
        return f

    # ---------------------------------------------------------------- time
    def _time_features(self, df: pd.DataFrame) -> pd.DataFrame:
        f = pd.DataFrame(index=df.index)
        idx = df.index
        if isinstance(idx, pd.DatetimeIndex):
            f["day_of_week"] = idx.dayofweek
            if hasattr(idx, "hour"):
                minutes_since_midnight = pd.Series(idx.hour * 60 + idx.minute, index=idx)
                f["minutes_since_open"] = (minutes_since_midnight - 9 * 60 - 15).clip(lower=0)
        return f

    # -------------------------------------------------------------- public
    def transform(self, df: pd.DataFrame, market_df: pd.DataFrame | None = None) -> pd.DataFrame:
        parts = [
            self._price_features(df),
            self._trend_features(df),
            self._volatility_features(df),
            self._volume_features(df),
            self._momentum_features(df),
            self._market_context_features(df, market_df),
            self._time_features(df),
        ]
        feats = pd.concat(parts, axis=1)
        feats.attrs["feature_version"] = self.feature_version
        return feats

    def feature_names(self) -> list[str]:
        """Dummy frame to enumerate feature columns without real data."""
        dummy = pd.DataFrame({
            "open": np.linspace(100, 110, 260), "high": np.linspace(101, 111, 260),
            "low": np.linspace(99, 109, 260), "close": np.linspace(100, 110, 260),
            "volume": np.random.randint(1000, 5000, 260),
        }, index=pd.date_range("2024-01-01", periods=260, freq="B"))
        return list(self.transform(dummy).columns)
