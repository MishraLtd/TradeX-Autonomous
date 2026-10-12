"""
Target definitions — Section 3.

TIMING CONVENTION (critical, read before touching this file):

  For every row `t` in a per-symbol OHLCV frame:
    - `decision_time(t)`  = the timestamp at which the model is asked to predict.
    - `entry_time(t)`     = the first tradable timestamp AFTER decision_time
                             (for delivery: next session open; for intraday:
                             next bar open — we do NOT allow entry at the
                             same bar's close, since that price is not
                             actionable at decision time).
    - `exit_time(t, h)`   = entry_time(t) shifted forward by horizon h.

  Every function below is written from the perspective of "what does row
  t look like once we know the label", but the FEATURE builder must only
  ever use data with timestamp <= decision_time(t). Target computation
  is intentionally allowed to look forward — that's what makes it a label.
  The leakage boundary is enforced at the dataset-assembly step
  (datasets/builder.py), not here.

All functions take a single-symbol, time-sorted OHLCV DataFrame with
columns: ['open','high','low','close','volume'] and a DatetimeIndex.
"""

from __future__ import annotations
import numpy as np
import pandas as pd


def close_to_close_return(df: pd.DataFrame, horizon_bars: int) -> pd.Series:
    """Naive target: close[t+h]/close[t] - 1.

    WARNING (Sec 3): this assumes you can transact AT the close, which is
    not realistic for a retail Zerodha Kite account. Kept only as a
    baseline / sanity-check target, not the primary training target.
    """
    fwd_close = df["close"].shift(-horizon_bars)
    return fwd_close / df["close"] - 1.0


def open_to_close_return(df: pd.DataFrame, horizon_bars: int) -> pd.Series:
    """entry at next bar's OPEN, exit at close[t+h].

    This is the more realistic default for delivery/swing: you decide
    using information available through close[t], but you can only
    execute at the NEXT session's open.
    """
    entry_px = df["open"].shift(-1)
    exit_px = df["close"].shift(-horizon_bars)
    return exit_px / entry_px - 1.0


def entry_to_exit_return(
    df: pd.DataFrame,
    horizon_bars: int,
    entry_lag_bars: int = 1,
    exit_price_col: str = "close",
) -> pd.Series:
    """General entry/exit target with an explicit, configurable entry lag
    (in bars) to model realistic order-placement latency, and a
    configurable exit price column (e.g. 'open' or 'close').

    entry_px  = price at t + entry_lag_bars
    exit_px   = exit_price_col at t + entry_lag_bars + horizon_bars
    """
    entry_px = df["open"].shift(-entry_lag_bars)
    exit_px = df[exit_price_col].shift(-(entry_lag_bars + horizon_bars))
    return exit_px / entry_px - 1.0


def forward_mfe_mae(
    df: pd.DataFrame,
    horizon_bars: int,
    entry_lag_bars: int = 1,
) -> pd.DataFrame:
    """Maximum Favourable / Adverse Excursion over the forward window,
    measured relative to the realistic entry price (next bar's open).

    Returns a DataFrame with columns ['mfe', 'mae'] (mae is <= 0).
    """
    entry_px = df["open"].shift(-entry_lag_bars)
    n = len(df)
    mfe = np.full(n, np.nan)
    mae = np.full(n, np.nan)
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    entry = entry_px.to_numpy()

    for i in range(n):
        start = i + entry_lag_bars
        end = start + horizon_bars
        if end > n or np.isnan(entry[i]):
            continue
        window_high = highs[start:end].max()
        window_low = lows[start:end].min()
        mfe[i] = window_high / entry[i] - 1.0
        mae[i] = window_low / entry[i] - 1.0

    return pd.DataFrame({"mfe": mfe, "mae": mae}, index=df.index)


def triple_barrier_label(
    df: pd.DataFrame,
    horizon_bars: int,
    take_profit_pct: float,
    stop_loss_pct: float,
    entry_lag_bars: int = 1,
) -> pd.DataFrame:
    """Lopez de Prado-style triple-barrier labeling.

    For each row: entry at next bar's open, then walk forward up to
    horizon_bars looking for the FIRST bar where high/low pierces the
    take-profit or stop-loss barrier. If neither is hit, the label is
    the return at the time (vertical) barrier.

    Returns columns: ['label' (1/-1/0), 'barrier_return', 'bars_to_hit',
    'hit_type' ('tp'/'sl'/'vertical')].

    NOTE: intrabar hit order (did TP or SL come first within the same
    bar?) is genuinely ambiguous from OHLC alone. We conservatively
    assume the WORSE outcome hits first (SL before TP) when a single
    bar touches both barriers — this avoids an optimistic bias.
    """
    n = len(df)
    entry_px = df["open"].shift(-entry_lag_bars).to_numpy()
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()

    label = np.zeros(n)
    barrier_return = np.full(n, np.nan)
    bars_to_hit = np.full(n, np.nan)
    hit_type = np.array([""] * n, dtype=object)

    for i in range(n):
        start = i + entry_lag_bars
        end = start + horizon_bars
        if end > n or np.isnan(entry_px[i]):
            continue
        e = entry_px[i]
        tp_level = e * (1 + take_profit_pct)
        sl_level = e * (1 - stop_loss_pct)
        hit = False
        for j in range(start, end):
            touched_tp = highs[j] >= tp_level
            touched_sl = lows[j] <= sl_level
            if touched_tp and touched_sl:
                # ambiguous same-bar: assume SL hits first (conservative)
                label[i] = -1
                barrier_return[i] = -stop_loss_pct
                bars_to_hit[i] = j - start + 1
                hit_type[i] = "sl"
                hit = True
                break
            elif touched_sl:
                label[i] = -1
                barrier_return[i] = -stop_loss_pct
                bars_to_hit[i] = j - start + 1
                hit_type[i] = "sl"
                hit = True
                break
            elif touched_tp:
                label[i] = 1
                barrier_return[i] = take_profit_pct
                bars_to_hit[i] = j - start + 1
                hit_type[i] = "tp"
                hit = True
                break
        if not hit:
            vert_ret = closes[end - 1] / e - 1.0
            barrier_return[i] = vert_ret
            bars_to_hit[i] = horizon_bars
            hit_type[i] = "vertical"
            label[i] = 1 if vert_ret > 0 else (-1 if vert_ret < 0 else 0)

    return pd.DataFrame(
        {"label": label, "barrier_return": barrier_return,
         "bars_to_hit": bars_to_hit, "hit_type": hit_type},
        index=df.index,
    )


def build_target_panel(
    df: pd.DataFrame,
    horizon_bars: int,
    take_profit_pct: float = 0.02,
    stop_loss_pct: float = 0.01,
    entry_lag_bars: int = 1,
) -> pd.DataFrame:
    """Convenience: compute all target variants at once for target-
    distribution analysis (Sec 8) and target-choice comparison (Sec 3)."""
    out = pd.DataFrame(index=df.index)
    out["target_close_to_close"] = close_to_close_return(df, horizon_bars)
    out["target_open_to_close"] = open_to_close_return(df, horizon_bars)
    out["target_entry_to_exit"] = entry_to_exit_return(df, horizon_bars, entry_lag_bars)
    mfe_mae = forward_mfe_mae(df, horizon_bars, entry_lag_bars)
    out["target_mfe"] = mfe_mae["mfe"]
    out["target_mae"] = mfe_mae["mae"]
    tb = triple_barrier_label(df, horizon_bars, take_profit_pct, stop_loss_pct, entry_lag_bars)
    out["target_tb_label"] = tb["label"]
    out["target_tb_return"] = tb["barrier_return"]
    out["target_tb_hit_type"] = tb["hit_type"]
    # last row(s) with insufficient forward data are NaN by construction —
    # these are dropped later at dataset-assembly, never imputed.
    return out
