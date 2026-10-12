from __future__ import annotations
from typing import List, Optional

from .enums import StressLevel
from .config import StressConfig
from ._math_utils import pct_change, zscore


def compute_stress(
    closes: List[float],
    volumes: List[float],
    volatility_percentile: Optional[float],
    breadth_ratio: Optional[float],
    avg_pairwise_correlation_history: Optional[List[float]] = None,
    avg_pairwise_correlation_now: Optional[float] = None,
    cfg: StressConfig = None,
):
    """Additive, transparent stress score (§16) - NOT `NIFTY < -2% = PANIC`.
    Every contributing factor is optional; missing factors simply don't add
    points rather than being imputed, so a data gap can only make the score
    look calmer than it should, never look falsely panicked... which is why
    validator.py separately checks freshness and can override to UNKNOWN/
    RESTRICT regardless of what the stress score says (§8, §44).
    """
    cfg = cfg or StressConfig()
    score = 0
    reasons = []
    n = len(closes)

    intraday_ret = pct_change(closes, 1) if n >= 2 else None
    if intraday_ret is not None:
        if intraday_ret <= cfg.intraday_return_severe:
            score += 2
            reasons.append(f"INTRADAY_RETURN_SEVERE({intraday_ret:.3f})")
        elif intraday_ret <= cfg.intraday_return_elevated:
            score += 1
            reasons.append(f"INTRADAY_RETURN_ELEVATED({intraday_ret:.3f})")

    if n >= 20:
        recent_high = max(closes[-20:])
        drawdown = (closes[-1] - recent_high) / recent_high if recent_high else None
        if drawdown is not None:
            if drawdown <= cfg.index_drawdown_severe:
                score += 2
                reasons.append(f"DRAWDOWN_SEVERE({drawdown:.3f})")
            elif drawdown <= cfg.index_drawdown_elevated:
                score += 1
                reasons.append(f"DRAWDOWN_ELEVATED({drawdown:.3f})")

    if volatility_percentile is not None:
        if volatility_percentile >= cfg.vol_percentile_severe:
            score += 2
            reasons.append(f"VOL_PCTL_SEVERE({volatility_percentile:.2f})")
        elif volatility_percentile >= cfg.vol_percentile_elevated:
            score += 1
            reasons.append(f"VOL_PCTL_ELEVATED({volatility_percentile:.2f})")

    if breadth_ratio is not None and breadth_ratio <= cfg.breadth_collapse_ratio:
        score += 2
        reasons.append(f"BREADTH_COLLAPSE({breadth_ratio:.2f})")

    if avg_pairwise_correlation_history and avg_pairwise_correlation_now is not None:
        z = zscore(avg_pairwise_correlation_history, avg_pairwise_correlation_now)
        if z is not None and z >= cfg.correlation_spike_z:
            score += 1
            reasons.append(f"CORRELATION_SPIKE(z={z:.2f})")

    if n >= 20 and len(volumes) >= 20:
        vol_hist = volumes[-20:-1]
        if vol_hist:
            z = zscore(vol_hist, volumes[-1])
            if z is not None and z >= cfg.volume_spike_z:
                score += 1
                reasons.append(f"VOLUME_SPIKE(z={z:.2f})")

    if score >= cfg.panic_score:
        level = StressLevel.PANIC
    elif score >= cfg.severe_score:
        level = StressLevel.SEVERE
    elif score >= cfg.elevated_score:
        level = StressLevel.ELEVATED
    else:
        level = StressLevel.NORMAL

    if not reasons:
        reasons.append("NO_STRESS_FACTORS_TRIGGERED")

    return level, score, reasons
