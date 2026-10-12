from __future__ import annotations
from typing import Optional

from .enums import BreadthState
from .config import BreadthConfig
from .schemas import BreadthSnapshot


def compute_breadth(snapshot: Optional[BreadthSnapshot], cfg: BreadthConfig):
    """Returns (BreadthState, ratio_used or None, reason_codes).

    Breadth is OPTIONAL DATA (§20) - many low-cost data sources won't have
    full advance/decline for NSE. When unavailable we return UNKNOWN rather
    than silently defaulting to NEUTRAL, so downstream never mistakes
    "we don't know" for "breadth is fine" (§8/§44).
    """
    if snapshot is None or not snapshot.is_available:
        return BreadthState.UNKNOWN, None, ["BREADTH_DATA_UNAVAILABLE"]

    ratio = snapshot.pct_above_sma50
    reasons = []
    if ratio is None and snapshot.advancing is not None and snapshot.declining is not None:
        total = snapshot.advancing + snapshot.declining
        ratio = snapshot.advancing / total if total > 0 else None
        reasons.append("BREADTH_FROM_ADV_DECL")
    else:
        reasons.append("BREADTH_FROM_PCT_ABOVE_SMA50")

    if ratio is None:
        return BreadthState.UNKNOWN, None, ["BREADTH_RATIO_UNCOMPUTABLE"]

    if ratio >= cfg.strong_positive_ratio:
        state = BreadthState.STRONG_POSITIVE
    elif ratio >= cfg.positive_ratio:
        state = BreadthState.POSITIVE
    elif ratio <= cfg.strong_negative_ratio:
        state = BreadthState.STRONG_NEGATIVE
    elif ratio <= cfg.negative_ratio:
        state = BreadthState.NEGATIVE
    else:
        state = BreadthState.NEUTRAL

    reasons.append(f"BREADTH_RATIO({ratio:.2f})")
    return state, ratio, reasons
