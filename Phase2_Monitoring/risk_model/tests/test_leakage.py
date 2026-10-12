import numpy as np
import pandas as pd
from risk_model import features, labels, validation
from risk_model.exceptions import LeakageError


def test_features_are_causal(ohlcv):
    """Mutating rows strictly AFTER index i must not change the computed
    feature values AT row i. This is the core look-ahead-bias test
    (Section 13)."""
    base = features.build_feature_frame(ohlcv)

    mutated = ohlcv.copy()
    cut = len(mutated) // 2
    # Blow up all future OHLCV values after `cut` — if any feature at or
    # before `cut` changes, it was peeking into the future.
    mutated.loc[cut + 1 :, ["open", "high", "low", "close"]] *= 5.0
    mutated.loc[cut + 1 :, "volume"] *= 10.0

    mutated_features = features.build_feature_frame(mutated)

    for col in features.FEATURE_COLUMNS:
        pd.testing.assert_series_equal(
            base[col].iloc[: cut + 1],
            mutated_features[col].iloc[: cut + 1],
            check_names=False,
        )


def test_label_requires_full_forward_horizon(ohlcv):
    """A trade entered too close to the end of available history must be
    dropped (right-censored), never silently zero-filled (Section 32)."""
    n = len(ohlcv)
    horizon = 10
    label = labels.compute_trade_label(
        ohlcv, entry_idx=n - 3, horizon_bars=horizon, stop_price=1.0, target_price=999.0
    )
    assert label is None


def test_label_frame_entry_before_window_end(ohlcv):
    lf = labels.build_label_frame(
        ohlcv, horizon_bars=5, stop_distance_pct=1.0, target_distance_pct=2.0, trade_type="DELIVERY"
    )
    assert len(lf) > 0
    assert (lf["target_window_end_timestamp"] > lf["entry_timestamp"]).all()


def test_purged_split_has_no_overlap(ohlcv):
    lf = labels.build_label_frame(
        ohlcv, horizon_bars=5, stop_distance_pct=1.0, target_distance_pct=2.0
    )
    splitter = validation.PurgedWalkForwardSplit(n_splits=3, embargo_bars=2)
    for train_idx, test_idx in splitter.split(lf):
        # should not raise
        validation.assert_no_train_test_overlap(lf, train_idx, test_idx)
        assert len(set(train_idx) & set(test_idx)) == 0


def test_assert_no_lookahead_detects_violation():
    df = pd.DataFrame(
        {
            "entry_ts": pd.to_datetime(["2024-01-01", "2024-01-02"]),
            "feature_ts": pd.to_datetime(["2023-12-31", "2024-01-05"]),  # 2nd row is a violation
        }
    )
    try:
        validation.assert_no_lookahead(df, "entry_ts", "feature_ts")
        assert False, "expected LeakageError"
    except LeakageError:
        pass
