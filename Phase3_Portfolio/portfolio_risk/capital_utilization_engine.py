"""
capital_utilization_engine.py
--------------------------------
Capital utilization risk (spec section 5.I).

Prevents the Portfolio Manager from committing essentially all available
capital when the resulting portfolio would have no meaningful liquidity
buffer.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PortfolioState


@dataclass
class CapitalUtilizationResult:
    invested_pct: float = 0.0
    cash_pct: float = 0.0
    capital_utilization_score: float = 0.0


def capital_utilization_risk(
    portfolio: PortfolioState,
    max_invested_pct: float,
    warn_invested_pct: float,
    min_cash_buffer_pct: float,
) -> CapitalUtilizationResult:
    equity = portfolio.portfolio_equity()
    if equity <= 0:
        return CapitalUtilizationResult()

    invested_pct = portfolio.invested_capital() / equity
    cash_pct = portfolio.available_cash / equity

    if invested_pct <= warn_invested_pct * 0.5:
        score = (invested_pct / (warn_invested_pct * 0.5)) * 25 if warn_invested_pct > 0 else 0.0
    elif invested_pct <= max_invested_pct:
        span = max(max_invested_pct - warn_invested_pct * 0.5, 1e-9)
        score = 25 + (invested_pct - warn_invested_pct * 0.5) / span * 50
    else:
        span = max(max_invested_pct, 1e-9)
        score = 75 + min((invested_pct - max_invested_pct) / span, 1.0) * 25

    # Additional penalty if cash buffer is below the required minimum,
    # even if invested_pct alone looks acceptable.
    if cash_pct < min_cash_buffer_pct:
        deficit_frac = (min_cash_buffer_pct - cash_pct) / min_cash_buffer_pct if min_cash_buffer_pct > 0 else 0
        score = min(100.0, score + deficit_frac * 25)

    return CapitalUtilizationResult(
        invested_pct=invested_pct,
        cash_pct=cash_pct,
        capital_utilization_score=max(0.0, min(100.0, score)),
    )
