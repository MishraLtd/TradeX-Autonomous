from __future__ import annotations
from typing import List

from .enums import MomentumState
from .config import MomentumConfig
from ._math_utils import pct_change


def compute_momentum(closes: List[float], cfg: MomentumConfig):
    """Multi-horizon momentum: average of 3 lookback windows (short/med/long
    fractions of cfg.lookback_bars) so a single noisy horizon can't dominate."""
    n = len(closes)
    horizons = [
        max(1, cfg.lookback_bars // 4),
        max(2, cfg.lookback_bars // 2),
        cfg.lookback_bars,
    ]
    reasons = []
    changes = []
    for h in horizons:
        c = pct_change(closes, h)
        if c is not None:
            changes.append(c)

    if not changes:
        return MomentumState.UNKNOWN, 0.0, ["INSUFFICIENT_HISTORY_MOMENTUM"]

    score = sum(changes) / len(changes)
    reasons.append(f"MULTI_HORIZON_RETURN({score:.4f})")

    if score > cfg.positive_threshold:
        state = MomentumState.POSITIVE
    elif score < cfg.negative_threshold:
        state = MomentumState.NEGATIVE
    else:
        state = MomentumState.NEUTRAL

    return state, score, reasons
