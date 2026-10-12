"""
Chronological, purged walk-forward validation.

Never use random train/test splitting on trade-level rows (Section 33).
When label windows overlap in time (common for multi-day delivery
horizons), naive chronological splitting still leaks: a training row whose
label window extends into the test period has "seen" test-period prices.
We purge those rows and add an embargo buffer (Section 34).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterator, Tuple
import pandas as pd

from .exceptions import LeakageError


@dataclass
class PurgedWalkForwardSplit:
    n_splits: int
    embargo_bars: int = 5

    def split(
        self, label_frame: pd.DataFrame
    ) -> Iterator[Tuple[pd.Index, pd.Index]]:
        """
        label_frame must contain 'entry_timestamp' and
        'target_window_end_timestamp' columns (see labels.build_label_frame).
        Yields (train_index, test_index) pairs in chronological order.
        """
        df = label_frame.sort_values("entry_timestamp").reset_index(drop=False)
        n = len(df)
        fold_size = n // (self.n_splits + 1)
        if fold_size < 1:
            raise ValueError("Not enough rows for the requested number of splits")

        for k in range(1, self.n_splits + 1):
            train_end = fold_size * k
            test_start = train_end
            test_end = min(train_end + fold_size, n)
            if test_start >= test_end:
                continue

            test_slice = df.iloc[test_start:test_end]
            test_period_start = test_slice["entry_timestamp"].min()

            train_slice = df.iloc[:train_end]
            # PURGE: drop any training row whose label window
            # (target_window_end_timestamp) extends into the test period.
            purge_mask = train_slice["target_window_end_timestamp"] < test_period_start
            purged_train = train_slice[purge_mask]

            # EMBARGO: also drop the last `embargo_bars` rows immediately
            # preceding the test period, to absorb residual serial
            # correlation not captured by the label window itself.
            if self.embargo_bars > 0 and len(purged_train) > self.embargo_bars:
                purged_train = purged_train.iloc[: -self.embargo_bars]

            yield purged_train["index"].to_numpy(), test_slice["index"].to_numpy()


def assert_no_lookahead(
    feature_frame: pd.DataFrame, entry_timestamp_col: str, feature_timestamp_col: str
) -> None:
    """Raises LeakageError if any feature row's timestamp is after the
    corresponding entry timestamp. This is meant to be run as an automated
    check (Section 14) whenever features are joined onto trade rows."""
    bad = feature_frame[feature_frame[feature_timestamp_col] > feature_frame[entry_timestamp_col]]
    if len(bad) > 0:
        raise LeakageError(
            f"{len(bad)} rows have a feature timestamp after the entry "
            f"timestamp — this is look-ahead leakage."
        )


def assert_no_train_test_overlap(
    label_frame: pd.DataFrame, train_idx, test_idx
) -> None:
    """Raises LeakageError if any training row's label window overlaps the
    entry timestamps present in the test set."""
    test_period_start = label_frame.loc[test_idx, "entry_timestamp"].min()
    train_rows = label_frame.loc[train_idx]
    overlap = train_rows[train_rows["target_window_end_timestamp"] >= test_period_start]
    if len(overlap) > 0:
        raise LeakageError(
            f"{len(overlap)} training rows have label windows overlapping "
            f"the test period — purge is insufficient."
        )
