"""
Label construction (Sections 5-10, 12-14).

Hard rule: everything in this file consumes data STRICTLY AFTER the entry
timestamp (the forward path). None of it may ever be fed into `features.py`
or into the live inference path — that would be label leakage. Each label
row carries explicit `entry_timestamp` and `target_window_end_timestamp`
fields precisely so `validation.py` can assert the leakage invariant
automatically (Section 14).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd


@dataclass
class TradeLabel:
    entry_timestamp: pd.Timestamp
    target_window_end_timestamp: pd.Timestamp
    entry_price: float
    final_return_pct: float
    mae_pct: float          # most negative excursion, always <= 0
    mfe_pct: float          # most positive excursion, always >= 0
    stop_hit: Optional[bool]
    target_hit: Optional[bool]
    bars_to_stop: Optional[int]
    bars_to_target: Optional[int]
    which_first: Optional[str]  # "STOP" | "TARGET" | "NEITHER"
    gap_down_pct: Optional[float] = None  # delivery only: (next_open - prev_close)/prev_close when negative


def _forward_window(df: pd.DataFrame, entry_idx: int, horizon_bars: int) -> pd.DataFrame:
    """Strictly-forward slice: bars entry_idx+1 .. entry_idx+horizon_bars inclusive.
    Entry bar itself is excluded from the *path* (the entry print is the
    reference price, not part of the excursion search) to avoid off-by-one
    leakage of the entry bar's own high/low into "excursion after entry"."""
    start = entry_idx + 1
    end = min(entry_idx + horizon_bars + 1, len(df))
    if start >= len(df):
        return df.iloc[0:0]
    return df.iloc[start:end]


def compute_trade_label(
    df: pd.DataFrame,
    entry_idx: int,
    horizon_bars: int,
    stop_price: float,
    target_price: float,
    entry_price: Optional[float] = None,
    trade_type: str = "DELIVERY",
) -> Optional[TradeLabel]:
    """
    df: single-symbol OHLCV, sorted ascending by timestamp, columns
        ['timestamp','open','high','low','close'].
    entry_idx: integer position of the entry bar in df.
    horizon_bars: how many *future* bars define the label window.

    Returns None if there isn't enough forward history to compute a label
    (this row must be dropped from training, never zero-filled — Section 32).
    """
    if entry_price is None:
        entry_price = float(df.iloc[entry_idx]["close"])

    fwd = _forward_window(df, entry_idx, horizon_bars)
    if len(fwd) == 0:
        return None
    # Require the FULL horizon to be observable, otherwise the label is
    # right-censored and must not be silently treated as "no event".
    if len(fwd) < horizon_bars:
        return None

    highs = fwd["high"].to_numpy(dtype=float)
    lows = fwd["low"].to_numpy(dtype=float)
    closes = fwd["close"].to_numpy(dtype=float)

    ret_path = (closes[-1] - entry_price) / entry_price * 100.0
    mae_path_pct = (lows.min() - entry_price) / entry_price * 100.0  # <=0 typically
    mfe_path_pct = (highs.max() - entry_price) / entry_price * 100.0  # >=0 typically
    mae_pct = min(0.0, mae_path_pct)
    mfe_pct = max(0.0, mfe_path_pct)

    # Race: does low<=stop or high>=target happen first, bar by bar.
    stop_hit_bar, target_hit_bar = None, None
    for i in range(len(fwd)):
        if stop_hit_bar is None and lows[i] <= stop_price:
            stop_hit_bar = i + 1  # bars-after-entry, 1-indexed
        if target_hit_bar is None and highs[i] >= target_price:
            target_hit_bar = i + 1
        if stop_hit_bar is not None and target_hit_bar is not None:
            break

    if stop_hit_bar is None and target_hit_bar is None:
        which_first = "NEITHER"
    elif stop_hit_bar is None:
        which_first = "TARGET"
    elif target_hit_bar is None:
        which_first = "STOP"
    else:
        which_first = "STOP" if stop_hit_bar <= target_hit_bar else "TARGET"

    gap_down_pct = None
    if trade_type == "DELIVERY" and len(fwd) >= 1:
        # overnight gap realized on the first forward bar's open vs entry
        first_open = float(fwd.iloc[0]["open"])
        gap = (first_open - entry_price) / entry_price * 100.0
        gap_down_pct = min(0.0, gap)

    return TradeLabel(
        entry_timestamp=df.iloc[entry_idx]["timestamp"],
        target_window_end_timestamp=fwd.iloc[-1]["timestamp"],
        entry_price=entry_price,
        final_return_pct=ret_path,
        mae_pct=mae_pct,
        mfe_pct=mfe_pct,
        stop_hit=which_first == "STOP",
        target_hit=which_first == "TARGET",
        bars_to_stop=stop_hit_bar,
        bars_to_target=target_hit_bar,
        which_first=which_first,
        gap_down_pct=gap_down_pct,
    )


def build_label_frame(
    df: pd.DataFrame,
    horizon_bars: int,
    stop_distance_pct: float,
    target_distance_pct: float,
    trade_type: str = "DELIVERY",
) -> pd.DataFrame:
    """Vectorized-ish driver: builds one label row per valid entry bar,
    using a *fixed* stop/target distance (%) applied at each bar's close.
    In production these distances come from the actual trade configuration
    (Return Model / strategy), not a global constant — this is a training
    utility for backtested label construction over historical bars."""
    rows = []
    n = len(df)
    for i in range(n):
        entry_price = float(df.iloc[i]["close"])
        stop_price = entry_price * (1 - stop_distance_pct / 100.0)
        target_price = entry_price * (1 + target_distance_pct / 100.0)
        label = compute_trade_label(
            df, i, horizon_bars, stop_price, target_price, entry_price, trade_type
        )
        if label is None:
            continue
        rows.append(
            dict(
                entry_idx=i,
                entry_timestamp=label.entry_timestamp,
                target_window_end_timestamp=label.target_window_end_timestamp,
                entry_price=label.entry_price,
                final_return_pct=label.final_return_pct,
                mae_pct=label.mae_pct,
                mfe_pct=label.mfe_pct,
                stop_hit=label.stop_hit,
                target_hit=label.target_hit,
                bars_to_stop=label.bars_to_stop,
                bars_to_target=label.bars_to_target,
                which_first=label.which_first,
                gap_down_pct=label.gap_down_pct,
                loss=label.final_return_pct < 0,
            )
        )
    return pd.DataFrame(rows)
