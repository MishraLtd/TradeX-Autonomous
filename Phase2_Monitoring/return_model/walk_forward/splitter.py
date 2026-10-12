"""
Walk-forward + purged/embargoed validation — Sections 4, 22, 23.

Why purging AND embargo, not just chronological split:

  A label at decision-time t for a 5-day-forward horizon is only fully
  known at t+5 trading days. If a training row's label window overlaps
  the validation period's decision timestamps, information from the
  validation period leaks backward into training through the label.
  Standard chronological split does NOT prevent this by itself.

  PURGING removes training rows whose label window overlaps the
  validation window.
  EMBARGO additionally removes a buffer AFTER the validation window
  before the next training block starts, since even non-overlapping
  labels near the boundary can share short-term autocorrelated
  features/regimes with the validation set.

This module produces expanding-window walk-forward folds with purge +
embargo applied per Section 22/23. It operates on a pre-sorted panel
DataFrame indexed by timestamp, with a `symbol` column, so pooled
cross-sectional data purges correctly per-timestamp regardless of how
many symbols share that timestamp.
"""

from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

from ..config import Horizon, LABEL_OVERLAP, EMBARGO_EXTRA_BARS


@dataclass
class Fold:
    train_idx: pd.Index
    val_idx: pd.Index
    test_idx: pd.Index
    train_range: tuple[pd.Timestamp, pd.Timestamp]
    val_range: tuple[pd.Timestamp, pd.Timestamp]
    test_range: tuple[pd.Timestamp, pd.Timestamp]


def _purge_embargo_bound(horizon: Horizon) -> pd.Timedelta:
    """Approximate purge+embargo width in calendar time. For daily-bar
    swing horizons this is in trading days (~1.4x calendar-day buffer
    for weekends); for intraday it's in minutes within a session.
    This is intentionally conservative — tune once real bar timestamps
    are available."""
    bars = LABEL_OVERLAP[horizon] + EMBARGO_EXTRA_BARS[horizon]
    if horizon.is_intraday:
        return pd.Timedelta(minutes=bars)
    return pd.Timedelta(days=int(bars * 1.6))  # buffer for weekends/holidays


def expanding_walk_forward_folds(
    panel: pd.DataFrame,
    horizon: Horizon,
    n_folds: int = 5,
    val_frac: float = 0.15,
    test_frac: float = 0.15,
) -> list[Fold]:
    """Expanding-window walk-forward split with purge + embargo.

    Timeline per fold k (k = 1..n_folds):
      TRAIN: [start, boundary_k - purge_embargo)
      VAL:   [boundary_k, boundary_k + val_window)
      TEST:  [boundary_k + val_window + embargo, boundary_k + val_window + embargo + test_window)

    Training window EXPANDS with each fold (uses all data prior to the
    purge boundary); validation/test windows slide forward.
    """
    timestamps = panel.index.unique().sort_values()
    n = len(timestamps)
    if n < 50:
        raise ValueError("Not enough distinct timestamps for walk-forward validation")

    buffer = _purge_embargo_bound(horizon)
    val_window_n = max(1, int(n * val_frac / n_folds))
    test_window_n = max(1, int(n * test_frac / n_folds))
    block = val_window_n + test_window_n
    start_search = int(n * 0.3)  # first ~30% reserved as minimum initial train

    folds: list[Fold] = []
    fold_starts = list(range(start_search, n - block, max(1, (n - block - start_search) // n_folds or 1)))[:n_folds]

    for fold_start in fold_starts:
        val_start_ts = timestamps[fold_start]
        val_end_idx = min(fold_start + val_window_n, n - 1)
        val_end_ts = timestamps[val_end_idx]
        test_start_idx = min(val_end_idx + 1, n - 1)
        test_end_idx = min(test_start_idx + test_window_n, n - 1)
        test_start_ts = timestamps[test_start_idx]
        test_end_ts = timestamps[test_end_idx]

        train_cut_ts = val_start_ts - buffer  # PURGE: drop train rows too close to val
        train_mask = panel.index < train_cut_ts
        val_mask = (panel.index >= val_start_ts) & (panel.index <= val_end_ts)
        test_mask = (panel.index >= test_start_ts + buffer) & (panel.index <= test_end_ts)  # EMBARGO before test

        train_idx = panel.index[train_mask]
        val_idx = panel.index[val_mask]
        test_idx = panel.index[test_mask]

        if len(train_idx) == 0 or len(val_idx) == 0 or len(test_idx) == 0:
            continue

        folds.append(Fold(
            train_idx=train_idx, val_idx=val_idx, test_idx=test_idx,
            train_range=(train_idx.min(), train_idx.max()),
            val_range=(val_idx.min(), val_idx.max()),
            test_range=(test_idx.min(), test_idx.max()),
        ))

    return folds
