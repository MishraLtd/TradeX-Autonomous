"""
correlation_engine.py
-----------------------
Pairwise correlation risk (spec section 5.C).

Uses Pearson correlation on aligned return histories. Handles:
  - missing history (position excluded from correlation matrix)
  - insufficient history (flagged, correlation still computed if
    >= a minimum floor, otherwise marked INSUFFICIENT_HISTORY)
  - unequal-length histories (aligned on the shortest common length,
    most-recent-aligned)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import numpy as np

from .models import Position, DataQualityReport, DataQualityState


@dataclass
class CorrelationResult:
    matrix: Dict[str, Dict[str, float]] = field(default_factory=dict)
    average_correlation: float = 0.0
    maximum_correlation: float = 0.0
    max_pair: Tuple[str, str] = ("", "")
    high_correlation_pairs: List[Tuple[str, str, float]] = field(default_factory=list)
    symbols_used: List[str] = field(default_factory=list)
    symbols_excluded: List[str] = field(default_factory=list)


def _align_histories(positions: List[Position], min_history_days: int):
    usable = {}
    excluded = []
    for p in positions:
        h = p.return_history
        if len(h) >= min_history_days:
            usable[p.symbol] = np.array(h[-max(len(h), min_history_days):], dtype=float)
        else:
            excluded.append(p.symbol)
    if len(usable) < 2:
        return usable, excluded, 0

    min_len = min(len(v) for v in usable.values())
    aligned = {sym: arr[-min_len:] for sym, arr in usable.items()}
    return aligned, excluded, min_len


def correlation_risk(
    positions: List[Position],
    min_history_days: int,
    high_corr_threshold: float,
    report: DataQualityReport,
) -> CorrelationResult:
    open_positions = [p for p in positions if not p.is_proposed]
    if len(open_positions) < 2:
        return CorrelationResult(symbols_used=[p.symbol for p in open_positions])

    aligned, excluded, min_len = _align_histories(open_positions, min_history_days)

    if excluded:
        report.downgrade(
            DataQualityState.LIMITED_DATA,
            f"correlation: excluded {excluded} — insufficient return history (<{min_history_days} obs)",
        )

    if len(aligned) < 2:
        report.downgrade(
            DataQualityState.INSUFFICIENT_HISTORY,
            "correlation: fewer than 2 positions have sufficient history — correlation matrix unavailable",
        )
        return CorrelationResult(symbols_excluded=excluded, symbols_used=list(aligned.keys()))

    symbols = list(aligned.keys())
    data = np.vstack([aligned[s] for s in symbols])

    with np.errstate(invalid="ignore", divide="ignore"):
        corr = np.corrcoef(data)

    corr = np.nan_to_num(corr, nan=0.0, posinf=1.0, neginf=-1.0)

    matrix = {symbols[i]: {symbols[j]: float(corr[i, j]) for j in range(len(symbols))} for i in range(len(symbols))}

    n = len(symbols)
    off_diag_vals = []
    high_pairs = []
    max_val = -1.0
    max_pair = ("", "")
    for i in range(n):
        for j in range(i + 1, n):
            v = corr[i, j]
            off_diag_vals.append(v)
            if v > max_val:
                max_val = v
                max_pair = (symbols[i], symbols[j])
            if v >= high_corr_threshold:
                high_pairs.append((symbols[i], symbols[j], float(v)))

    avg_corr = float(np.mean(off_diag_vals)) if off_diag_vals else 0.0

    if min_len < 60:
        report.downgrade(
            DataQualityState.LIMITED_DATA,
            f"correlation: only {min_len} aligned observations — estimates may be unstable",
        )

    return CorrelationResult(
        matrix=matrix,
        average_correlation=avg_corr,
        maximum_correlation=float(max_val) if off_diag_vals else 0.0,
        max_pair=max_pair,
        high_correlation_pairs=sorted(high_pairs, key=lambda x: -x[2]),
        symbols_used=symbols,
        symbols_excluded=excluded,
    )


def correlation_score(avg_corr: float, max_corr: float, warn_avg: float, max_avg: float) -> float:
    """0-100 score blending average and peak pairwise correlation."""
    def ramp(val, warn, cap):
        if val <= warn * 0.5:
            return (val / (warn * 0.5)) * 25 if warn > 0 else 0.0
        elif val <= cap:
            span = max(cap - warn * 0.5, 1e-9)
            return 25 + (val - warn * 0.5) / span * 50
        else:
            span = max(cap, 1e-9)
            return 75 + min((val - cap) / span, 1.0) * 25

    avg_score = ramp(max(avg_corr, 0.0), warn_avg, max_avg)
    peak_score = ramp(max(max_corr, 0.0), warn_avg, max_avg)
    # weight peak correlation slightly higher — a single tight pair still matters
    return max(0.0, min(100.0, 0.4 * avg_score + 0.6 * peak_score))
