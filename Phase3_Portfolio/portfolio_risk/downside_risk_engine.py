"""
downside_risk_engine.py
-------------------------
Downside risk (spec section 5.E) — distinguishes expected risk (symmetric
volatility, handled in covariance_engine) from downside/tail risk.

Metrics:
  - downside_deviation: semi-deviation of portfolio historical returns
    below a minimum acceptable return (MAR = 0 by default)
  - expected_loss: mean of negative portfolio return observations
  - max_observed_adverse_move: worst single-period observed portfolio return
  - stop_loss_based_downside: delegated to stop_loss_risk_engine, referenced
    here only as a cross-check field
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .models import Position, DataQualityReport, DataQualityState


@dataclass
class DownsideRiskResult:
    downside_deviation: float = 0.0          # daily, fractional
    expected_loss: float = 0.0               # mean of negative-return days
    max_observed_adverse_move: float = 0.0   # worst single-day portfolio return (negative)
    observations_used: int = 0
    downside_score: float = 0.0              # 0-100


def _build_portfolio_return_series(
    positions: List[Position],
    portfolio_equity: float,
) -> Optional[np.ndarray]:
    open_positions = [p for p in positions if not p.is_proposed and p.return_history]
    if not open_positions or portfolio_equity <= 0:
        return None

    min_len = min(len(p.return_history) for p in open_positions)
    if min_len == 0:
        return None

    weights = np.array([max(p.market_value, 0.0) / portfolio_equity for p in open_positions])
    returns = np.vstack([np.array(p.return_history[-min_len:], dtype=float) for p in open_positions])
    portfolio_returns = weights @ returns
    return portfolio_returns


def downside_risk(
    positions: List[Position],
    portfolio_equity: float,
    min_history_days: int,
    report: DataQualityReport,
    mar: float = 0.0,
) -> DownsideRiskResult:
    series = _build_portfolio_return_series(positions, portfolio_equity)

    if series is None or len(series) < min_history_days:
        report.downgrade(
            DataQualityState.INSUFFICIENT_HISTORY,
            "downside risk: insufficient portfolio return history — downside deviation unavailable",
        )
        return DownsideRiskResult(observations_used=0 if series is None else len(series))

    downside_obs = series[series < mar]
    downside_deviation = float(np.sqrt(np.mean((downside_obs - mar) ** 2))) if len(downside_obs) > 0 else 0.0
    expected_loss = float(np.mean(downside_obs)) if len(downside_obs) > 0 else 0.0
    max_adverse = float(np.min(series)) if len(series) > 0 else 0.0

    # Score: annualize downside deviation for comparability with volatility limits.
    annualized_dd = downside_deviation * np.sqrt(252)
    score = min(100.0, (annualized_dd / 0.30) * 100)  # 30% annualized downside dev ~ max reasonable

    return DownsideRiskResult(
        downside_deviation=downside_deviation,
        expected_loss=expected_loss,
        max_observed_adverse_move=max_adverse,
        observations_used=len(series),
        downside_score=max(0.0, score),
    )
