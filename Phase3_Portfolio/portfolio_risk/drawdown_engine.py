"""
drawdown_engine.py
--------------------
Drawdown risk (spec section 5.H).

Consumes portfolio equity-curve-derived fields already tracked by the
Portfolio Manager (peak value, current value) rather than recomputing an
equity curve from scratch — per spec section 32 ("reuse existing
schemas/calculations where appropriate"). If a historical equity-value
series is supplied, rolling drawdown and drawdown acceleration are also
computed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

from .models import PortfolioState, DataQualityReport, DataQualityState


@dataclass
class DrawdownResult:
    current_drawdown: Optional[float] = None
    max_drawdown: Optional[float] = None
    rolling_drawdown: Optional[float] = None
    drawdown_acceleration: Optional[float] = None  # change in drawdown over recent window
    distance_from_peak: Optional[float] = None
    state: str = "NORMAL"  # NORMAL | ELEVATED | HIGH | CRITICAL
    drawdown_score: float = 0.0


def classify_drawdown(dd: float, states: Dict[str, tuple]) -> str:
    for name, (lo, hi) in states.items():
        if lo <= dd < hi:
            return name
    return "CRITICAL"


def drawdown_risk(
    portfolio: PortfolioState,
    states: Dict[str, tuple],
    equity_history: Optional[List[float]] = None,
    rolling_window: int = 20,
    report: Optional[DataQualityReport] = None,
) -> DrawdownResult:
    current_dd = portfolio.current_drawdown()

    if current_dd is None:
        if report is not None:
            report.downgrade(
                DataQualityState.LIMITED_DATA,
                "drawdown: no peak_portfolio_value supplied — current drawdown unavailable",
            )
        return DrawdownResult()

    max_dd = current_dd
    rolling_dd = None
    acceleration = None

    if equity_history and len(equity_history) >= 2:
        eq = np.array(equity_history, dtype=float)
        running_peak = np.maximum.accumulate(eq)
        # avoid divide by zero
        running_peak_safe = np.where(running_peak == 0, 1e-9, running_peak)
        dd_series = (running_peak_safe - eq) / running_peak_safe
        max_dd = float(max(max_dd, np.max(dd_series)))

        window = dd_series[-rolling_window:] if len(dd_series) >= rolling_window else dd_series
        rolling_dd = float(np.max(window))

        if len(dd_series) >= 2:
            recent_half = dd_series[-max(rolling_window // 2, 1):]
            prior_half = dd_series[-rolling_window:-max(rolling_window // 2, 1)] if len(dd_series) >= rolling_window else dd_series[:len(dd_series)//2]
            if len(prior_half) > 0 and len(recent_half) > 0:
                acceleration = float(np.mean(recent_half) - np.mean(prior_half))

    state = classify_drawdown(current_dd, states)

    # Score: map state bands proportionally onto 0-100, then fine-tune within band.
    band_scores = {"NORMAL": (0, 25), "ELEVATED": (25, 50), "HIGH": (50, 80), "CRITICAL": (80, 100)}
    lo_band, hi_band = states.get(state, (0, 1))
    lo_score, hi_score = band_scores.get(state, (80, 100))
    if hi_band > lo_band:
        frac = min(max((current_dd - lo_band) / (hi_band - lo_band), 0.0), 1.0)
    else:
        frac = 1.0
    score = lo_score + frac * (hi_score - lo_score)

    distance_from_peak = None
    if portfolio.peak_portfolio_value:
        current_value = portfolio.current_portfolio_value or portfolio.portfolio_equity()
        distance_from_peak = portfolio.peak_portfolio_value - current_value

    return DrawdownResult(
        current_drawdown=current_dd,
        max_drawdown=max_dd,
        rolling_drawdown=rolling_dd,
        drawdown_acceleration=acceleration,
        distance_from_peak=distance_from_peak,
        state=state,
        drawdown_score=max(0.0, min(100.0, score)),
    )
