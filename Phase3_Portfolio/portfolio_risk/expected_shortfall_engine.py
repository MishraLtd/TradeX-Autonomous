"""
expected_shortfall_engine.py
------------------------------
Expected Shortfall / CVaR (spec section 5.G).

ES = E[L | L > VaR]

Computed from the same historical portfolio return series used for
historical VaR. Requires enough observations beyond the VaR breach point
to be meaningful; if fewer than a small minimum number of tail
observations exist, the estimate is flagged as LIMITED_DATA rather than
silently reported as precise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np

from .models import Position, DataQualityReport, DataQualityState
from .downside_risk_engine import _build_portfolio_return_series


@dataclass
class ExpectedShortfallResult:
    es: Dict[float, float] = field(default_factory=dict)  # confidence -> loss fraction
    tail_observations: Dict[float, int] = field(default_factory=dict)
    available: bool = False


MIN_TAIL_OBSERVATIONS = 5


def compute_expected_shortfall(
    positions: List[Position],
    portfolio_equity: float,
    confidence_levels: List[float],
    min_history_days: int,
    report: DataQualityReport,
) -> ExpectedShortfallResult:
    series = _build_portfolio_return_series(positions, portfolio_equity)
    result = ExpectedShortfallResult()

    if series is None or len(series) < min_history_days:
        report.downgrade(
            DataQualityState.INSUFFICIENT_HISTORY,
            "Expected Shortfall: insufficient portfolio return history — ES unavailable",
        )
        return result

    for cl in confidence_levels:
        pct = (1 - cl) * 100
        var_threshold = np.percentile(series, pct)
        tail = series[series <= var_threshold]
        n_tail = len(tail)
        result.tail_observations[cl] = n_tail
        if n_tail < MIN_TAIL_OBSERVATIONS:
            report.downgrade(
                DataQualityState.LIMITED_DATA,
                f"Expected Shortfall @ {cl:.0%}: only {n_tail} tail observations — estimate is unreliable",
            )
        loss = max(0.0, -float(np.mean(tail))) if n_tail > 0 else max(0.0, -float(var_threshold))
        result.es[cl] = loss

    result.available = True
    return result
