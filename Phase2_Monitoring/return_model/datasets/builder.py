"""
Dataset assembly — Sections 4, 5, 19, 23.

This is where the leakage boundary is actually enforced:
  - features(t) computed from data with timestamp <= t
  - targets(t) computed from data with timestamp > t
  - a row is included only if the symbol was a FILTER2.0-eligible member
    of the universe AT timestamp t (historical membership, not today's
    list) — prevents survivorship bias (Section 5).
"""

from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

from ..features.engine import FeatureEngine
from ..targets.definitions import build_target_panel
from ..config import Horizon, LABEL_OVERLAP


HORIZON_TO_BARS = {
    # Placeholder mapping — must be calibrated to the actual bar
    # frequency of the ingested data (e.g. 1-min bars for intraday,
    # daily bars for swing). Shown here assuming 1-min intraday bars
    # and daily swing bars, matching FILTER2.0's existing data cadence.
    Horizon.INTRA_30M: 30, Horizon.INTRA_60M: 60, Horizon.INTRA_120M: 120,
    Horizon.INTRA_EOD: 375,
    Horizon.SWING_1D: 1, Horizon.SWING_2D: 2, Horizon.SWING_3D: 3,
    Horizon.SWING_5D: 5, Horizon.SWING_10D: 10,
}


@dataclass
class SymbolUniverseHistory:
    """Historical eligibility windows for one symbol, as produced by
    FILTER2.0. `eligible_ranges` is a list of (start, end) timestamp
    pairs during which the symbol was part of the tradeable universe."""
    symbol: str
    eligible_ranges: list[tuple[pd.Timestamp, pd.Timestamp]]

    def is_eligible(self, ts: pd.Timestamp) -> bool:
        return any(start <= ts <= end for start, end in self.eligible_ranges)


def build_symbol_panel(
    symbol: str,
    ohlcv: pd.DataFrame,
    horizon: Horizon,
    feature_engine: FeatureEngine,
    market_df: pd.DataFrame | None = None,
    universe_history: SymbolUniverseHistory | None = None,
    take_profit_pct: float = 0.02,
    stop_loss_pct: float = 0.01,
) -> pd.DataFrame:
    """Build the leakage-safe (features, targets) panel for one symbol
    and one horizon. Rows are dropped, never imputed, when either the
    feature window or the label window is incomplete.
    """
    horizon_bars = HORIZON_TO_BARS[horizon]

    features = feature_engine.transform(ohlcv, market_df=market_df)
    targets = build_target_panel(ohlcv, horizon_bars, take_profit_pct, stop_loss_pct)

    panel = features.join(targets, how="inner")
    panel.insert(0, "symbol", symbol)
    panel.insert(1, "horizon", horizon.value)

    if universe_history is not None:
        panel["universe_eligible"] = [universe_history.is_eligible(ts) for ts in panel.index]
        panel = panel[panel["universe_eligible"]]

    # Drop rows where the primary training target is NaN (insufficient
    # forward data, e.g. the last `horizon_bars` rows of history).
    panel = panel.dropna(subset=["target_entry_to_exit"])
    return panel


def build_pooled_panel(
    symbol_data: dict[str, pd.DataFrame],
    horizon: Horizon,
    feature_engine: FeatureEngine,
    market_df: pd.DataFrame | None = None,
    universe_histories: dict[str, SymbolUniverseHistory] | None = None,
) -> pd.DataFrame:
    """Pooled cross-sectional panel across many symbols (Section 15).
    Each symbol contributes its own leakage-safe rows; pooling just
    concatenates them — it does NOT let one symbol's future leak into
    another symbol's row (they're independent time series joined only
    by shared calendar timestamps for market-context features).
    """
    frames = []
    for sym, ohlcv in symbol_data.items():
        uh = universe_histories.get(sym) if universe_histories else None
        frames.append(build_symbol_panel(sym, ohlcv, horizon, feature_engine, market_df, uh))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=0).sort_index()
