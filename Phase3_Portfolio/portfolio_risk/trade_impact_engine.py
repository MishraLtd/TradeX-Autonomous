"""
trade_impact_engine.py
--------------------------
"What happens if I add this trade?" (spec section 11).

Runs the full portfolio risk assessment twice — once on the current
portfolio, once with the proposed position(s) added — and returns a diff.
This is the primary interface Position Sizing / Portfolio Manager should
call before finalizing any new trade.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Dict, List

from .models import PortfolioState, Position


@dataclass
class TradeImpactResult:
    before_score: float = 0.0
    after_score: float = 0.0
    score_change: float = 0.0
    before_risk_class: str = ""
    after_risk_class: str = ""
    breached_limits_after: List[str] = field(default_factory=list)
    new_breaches_caused_by_trade: List[str] = field(default_factory=list)
    risk_contribution_of_new_position_pct: float = 0.0
    final_recommendation: str = ""
    before_full_report: Dict = field(default_factory=dict)
    after_full_report: Dict = field(default_factory=dict)


def evaluate_trade_impact(
    portfolio: PortfolioState,
    proposed_positions: List[Position],
    assess_fn,  # callable: PortfolioState -> dict (full schema), injected to avoid circular import
) -> TradeImpactResult:
    before_portfolio = deepcopy(portfolio)
    before_portfolio.positions = [p for p in before_portfolio.positions if not p.is_proposed]
    before_report = assess_fn(before_portfolio)

    # The proposed position(s) must be evaluated as if already open — every
    # risk engine in this package only considers `is_proposed=False`
    # positions "current" for portfolio-level math (weights, covariance,
    # etc). Clear the flag on a deep copy so the caller's objects and the
    # `before` assessment are unaffected.
    added = deepcopy(proposed_positions)
    for p in added:
        p.is_proposed = False

    after_portfolio = deepcopy(portfolio)
    after_portfolio.positions = (
        [p for p in after_portfolio.positions if not p.is_proposed] + added
    )
    after_report = assess_fn(after_portfolio)

    before_score = before_report["portfolio_risk_score"]
    after_score = after_report["portfolio_risk_score"]

    before_breaches = set(k for k, v in before_report["risk_limits"].items() if v == "BREACH")
    after_breaches = set(k for k, v in after_report["risk_limits"].items() if v == "BREACH")
    new_breaches = sorted(after_breaches - before_breaches)

    risk_contribution = 0.0
    marginal = after_report.get("marginal_risk", {}).get("risk_contribution_pct", {})
    for p in proposed_positions:
        risk_contribution += marginal.get(p.symbol, 0.0)

    if new_breaches:
        rec = (
            f"REJECT: adding this trade causes new limit breach(es): {', '.join(new_breaches)}. "
            f"Score would move {before_score:.1f} -> {after_score:.1f}."
        )
    elif after_score - before_score > 15:
        rec = (
            f"CAUTION: trade does not breach a hard limit but materially raises portfolio risk "
            f"({before_score:.1f} -> {after_score:.1f}). Consider a smaller size."
        )
    elif after_score < before_score:
        rec = (
            f"IMPROVES diversification/risk profile ({before_score:.1f} -> {after_score:.1f}). "
            f"No objection on portfolio-risk grounds."
        )
    else:
        rec = f"ACCEPTABLE: score moves {before_score:.1f} -> {after_score:.1f}, within policy."

    return TradeImpactResult(
        before_score=before_score,
        after_score=after_score,
        score_change=after_score - before_score,
        before_risk_class=before_report["risk_class"],
        after_risk_class=after_report["risk_class"],
        breached_limits_after=sorted(after_breaches),
        new_breaches_caused_by_trade=new_breaches,
        risk_contribution_of_new_position_pct=risk_contribution,
        final_recommendation=rec,
        before_full_report=before_report,
        after_full_report=after_report,
    )
