"""
var_engine.py
--------------
Portfolio Value at Risk (spec section 5.F).

Supports:
  - Historical VaR: empirical quantile of the historical portfolio return
    series (no distributional assumption).
  - Parametric VaR: assumes normally distributed returns, using the
    covariance-based portfolio volatility. Provided as a secondary,
    faster-to-compute cross-check — NOT presented as more accurate than
    historical VaR, and both are surfaced together.

VaR is expressed as a POSITIVE fraction of portfolio equity representing
the loss magnitude (e.g. var_95 = 0.032 means a 3.2% loss at 95% confidence).
This is not a guarantee — it is explicitly documented as a probabilistic
estimate under historical/normal-approximation assumptions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from scipy import stats as _stats  # numpy has no inverse-normal CDF; scipy ships with numpy stack

from .models import Position, DataQualityReport, DataQualityState
from .downside_risk_engine import _build_portfolio_return_series


@dataclass
class VaRResult:
    historical_var: Dict[float, float] = field(default_factory=dict)   # confidence -> loss fraction
    parametric_var: Dict[float, float] = field(default_factory=dict)
    method_used: str = "UNAVAILABLE"  # HISTORICAL | PARAMETRIC_ONLY | UNAVAILABLE
    observations_used: int = 0


def _z_score(confidence: float) -> float:
    try:
        return float(_stats.norm.ppf(confidence))
    except Exception:
        # Fallback approximations if scipy unavailable for some reason
        table = {0.90: 1.2816, 0.95: 1.6449, 0.99: 2.3263}
        return table.get(round(confidence, 2), 1.6449)


def compute_var(
    positions: List[Position],
    portfolio_equity: float,
    confidence_levels: List[float],
    min_history_days: int,
    horizon_days: int,
    portfolio_volatility_daily: Optional[float],
    report: DataQualityReport,
) -> VaRResult:
    series = _build_portfolio_return_series(positions, portfolio_equity)
    result = VaRResult()

    horizon_scale = float(np.sqrt(max(horizon_days, 1)))

    if series is not None and len(series) >= min_history_days:
        for cl in confidence_levels:
            # loss = -return at the (1-cl) percentile
            pct = (1 - cl) * 100
            quantile_return = float(np.percentile(series, pct))
            loss = max(0.0, -quantile_return) * horizon_scale
            result.historical_var[cl] = loss
        result.observations_used = len(series)
        result.method_used = "HISTORICAL"
        if len(series) < 60:
            report.downgrade(
                DataQualityState.LIMITED_DATA,
                f"VaR: only {len(series)} observations — historical VaR tail estimate is unstable",
            )
    else:
        report.downgrade(
            DataQualityState.INSUFFICIENT_HISTORY,
            "VaR: insufficient portfolio return history — historical VaR unavailable, using parametric only",
        )

    if portfolio_volatility_daily is not None and portfolio_volatility_daily > 0:
        for cl in confidence_levels:
            z = _z_score(cl)
            result.parametric_var[cl] = z * portfolio_volatility_daily * horizon_scale
        if result.method_used == "UNAVAILABLE":
            result.method_used = "PARAMETRIC_ONLY"
    elif result.method_used == "UNAVAILABLE":
        report.downgrade(
            DataQualityState.UNAVAILABLE,
            "VaR: neither historical returns nor portfolio volatility available — VaR cannot be computed",
        )

    return result


def var_score(var_95_pct_of_capital: float, warn: float, cap: float) -> float:
    if var_95_pct_of_capital <= warn * 0.5:
        return (var_95_pct_of_capital / (warn * 0.5)) * 25 if warn > 0 else 0.0
    elif var_95_pct_of_capital <= cap:
        span = max(cap - warn * 0.5, 1e-9)
        return 25 + (var_95_pct_of_capital - warn * 0.5) / span * 50
    else:
        span = max(cap, 1e-9)
        return max(0.0, min(100.0, 75 + min((var_95_pct_of_capital - cap) / span, 1.0) * 25))
