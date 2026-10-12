"""
marginal_risk_engine.py
--------------------------
Marginal Contribution to Risk (MCTR) and Component Contribution to Risk
(spec section 12).

Standard result for variance-based risk (Euler decomposition of portfolio
volatility):

    MCTR_i = (Sigma @ w)_i / sigma_p
    CCTR_i = w_i * MCTR_i
    %CCTR_i = CCTR_i / sigma_p

Sum of %CCTR_i across all i equals 1.0 (100%) by construction, which lets
us directly compare "capital weight" vs "risk contribution %" per spec's
worked example.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

import numpy as np

from .models import Position
from .covariance_engine import CovarianceResult


@dataclass
class MarginalRiskResult:
    capital_weight: Dict[str, float] = field(default_factory=dict)
    risk_contribution_pct: Dict[str, float] = field(default_factory=dict)
    disproportionate_symbols: List[str] = field(default_factory=list)  # risk% notably > capital weight%


def marginal_risk_contribution(
    positions: List[Position],
    portfolio_equity: float,
    cov_result: CovarianceResult,
    disproportion_threshold: float = 1.5,  # risk% / weight% ratio that counts as "disproportionate"
) -> MarginalRiskResult:
    open_positions = {p.symbol: p for p in positions if not p.is_proposed}
    if not open_positions or portfolio_equity <= 0 or cov_result.covariance_matrix is None or not cov_result.symbols:
        return MarginalRiskResult()

    symbols = cov_result.symbols
    weights = np.array(
        [max(open_positions[s].market_value, 0.0) / portfolio_equity if s in open_positions else 0.0
         for s in symbols]
    )
    sigma = cov_result.covariance_matrix

    portfolio_variance = float(weights @ sigma @ weights)
    if portfolio_variance <= 0:
        return MarginalRiskResult(
            capital_weight={s: float(w) for s, w in zip(symbols, weights)},
        )
    portfolio_vol = np.sqrt(portfolio_variance)

    marginal = sigma @ weights / portfolio_vol         # MCTR_i
    component = weights * marginal                      # CCTR_i
    pct_component = component / portfolio_vol            # %CCTR_i, sums to 1.0

    capital_weight = {s: float(w) for s, w in zip(symbols, weights)}
    risk_contribution_pct = {s: float(c) for s, c in zip(symbols, pct_component)}

    disproportionate = []
    total_invested_weight = float(np.sum(weights))
    for s in symbols:
        # Compare risk share against this position's share of INVESTED
        # capital, not total portfolio equity. risk_contribution_pct sums
        # to 1.0 across invested positions only (cash carries zero risk),
        # so comparing it against a weight normalized over total equity
        # (which includes idle cash and therefore sums to < 1.0 whenever
        # the portfolio isn't fully invested) would flag nearly every
        # position as "disproportionate" any time there's meaningful cash
        # on the sidelines — that was a real bug, not a feature.
        w_total_equity = capital_weight[s]
        if total_invested_weight <= 1e-9:
            continue
        w_of_invested = w_total_equity / total_invested_weight
        r = risk_contribution_pct[s]
        if w_of_invested > 1e-6 and r / w_of_invested >= disproportion_threshold:
            disproportionate.append(s)

    return MarginalRiskResult(
        capital_weight=capital_weight,
        risk_contribution_pct=risk_contribution_pct,
        disproportionate_symbols=disproportionate,
    )
