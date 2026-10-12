"""
recommendation_engine.py
---------------------------
Produces the final PORTFOLIO_RISK_STATUS decision (spec section 10) and
human-readable, metric-grounded recommendations (spec section 28).

Final safety logic (spec section 29): if there is a conflict between higher
expected return and unacceptable portfolio risk, the risk constraint always
wins — this module never looks at expected return when deciding status; it
is purely a function of realized risk metrics and limit breaches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from .risk_limit_engine import RiskLimitReport, LimitStatus
from .scoring_engine import ScoringResult
from .drawdown_engine import DrawdownResult
from .marginal_risk_engine import MarginalRiskResult


class PortfolioRiskStatus(str, Enum):
    ACCEPT = "ACCEPT"
    ACCEPT_WITH_WARNING = "ACCEPT_WITH_WARNING"
    REDUCE_EXPOSURE = "REDUCE_EXPOSURE"
    REJECT_NEW_POSITION = "REJECT_NEW_POSITION"
    EMERGENCY_REDUCTION = "EMERGENCY_REDUCTION"


@dataclass
class RecommendationResult:
    status: PortfolioRiskStatus = PortfolioRiskStatus.ACCEPT
    primary_drivers: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)
    explanation: str = ""


_LIMIT_LABELS = {
    "position_concentration": "Position concentration",
    "sector_concentration": "Sector concentration",
    "aggregate_stop_loss_risk": "Aggregate stop-loss exposure",
    "portfolio_volatility": "Portfolio volatility",
    "var_95": "Value at Risk (95%)",
    "expected_shortfall_95": "Expected Shortfall (95%)",
    "position_count": "Number of open positions",
    "average_correlation": "Average portfolio correlation",
    "max_pairwise_correlation": "Maximum pairwise correlation",
    "invested_capital_pct": "Invested capital %",
    "cash_buffer": "Cash buffer",
    "drawdown": "Portfolio drawdown",
    "stress_loss": "Worst-case stress-test loss",
    "liquidity_ratio": "Liquidity (position size vs traded value)",
}

_LIMIT_RECOMMENDATIONS = {
    "position_concentration": "Reduce the largest position before adding new exposure.",
    "sector_concentration": "Avoid adding further positions in the most concentrated sector.",
    "aggregate_stop_loss_risk": "Tighten stops or reduce position sizes to lower aggregate worst-case loss.",
    "portfolio_volatility": "Reduce weight in the highest-volatility positions.",
    "var_95": "Reduce overall exposure to bring 1-day VaR within policy.",
    "expected_shortfall_95": "Reduce tail exposure — consider trimming the largest contributors to loss.",
    "position_count": "Avoid opening further simultaneous positions until existing ones are closed.",
    "average_correlation": "Reduce correlated exposure before increasing total positions.",
    "max_pairwise_correlation": "Close or reduce one side of the most correlated pair.",
    "invested_capital_pct": "Hold more cash — invested capital is too high relative to policy.",
    "cash_buffer": "Increase idle cash buffer before taking on new risk.",
    "drawdown": "De-risk until the portfolio recovers from the current drawdown.",
    "stress_loss": "Reduce exposure that drives the worst stress scenario.",
    "liquidity_ratio": "Reduce position size in illiquid names relative to their traded volume.",
}


def _limit_explanation_line(name: str, check) -> str:
    label = _LIMIT_LABELS.get(name, name)
    return f"{label}: {check.value:.2%}" if abs(check.value) < 10 else f"{label}: {check.value:.2f}"


def build_recommendation(
    limit_report: RiskLimitReport,
    scoring: ScoringResult,
    drawdown: DrawdownResult,
    marginal_risk: MarginalRiskResult,
) -> RecommendationResult:
    breaches = limit_report.breaches()
    warnings = limit_report.warnings()

    # ---- Determine status -------------------------------------------------
    critical_breach_categories = {"drawdown", "var_95", "expected_shortfall_95", "stress_loss"}
    has_critical_breach = any(b in critical_breach_categories for b in breaches) or drawdown.state == "CRITICAL"

    if has_critical_breach or scoring.risk_class == "CRITICAL":
        status = PortfolioRiskStatus.EMERGENCY_REDUCTION
    elif breaches:
        # non-critical breach(es) -> reduce exposure if already invested-risk related,
        # else reject any *new* position while flagging existing exposure.
        if len(breaches) >= 3 or scoring.risk_class in ("SEVERE",):
            status = PortfolioRiskStatus.REDUCE_EXPOSURE
        else:
            status = PortfolioRiskStatus.REJECT_NEW_POSITION
    elif warnings:
        status = PortfolioRiskStatus.ACCEPT_WITH_WARNING
    else:
        status = PortfolioRiskStatus.ACCEPT

    # ---- Primary drivers ----------------------------------------------------
    drivers = []
    for name in breaches:
        check = limit_report.checks[name]
        drivers.append(f"{_LIMIT_LABELS.get(name, name)} BREACHED: {_fmt(check.value)}")
    for name in warnings:
        check = limit_report.checks[name]
        drivers.append(f"{_LIMIT_LABELS.get(name, name)} elevated (warning): {_fmt(check.value)}")

    if marginal_risk.disproportionate_symbols:
        drivers.append(
            f"Disproportionate risk contribution from: {', '.join(marginal_risk.disproportionate_symbols)}"
        )

    if not drivers:
        drivers.append("All monitored risk limits within policy.")

    # ---- Recommendations ------------------------------------------------
    recs = []
    for name in breaches + warnings:
        rec = _LIMIT_RECOMMENDATIONS.get(name)
        if rec and rec not in recs:
            recs.append(rec)

    if marginal_risk.disproportionate_symbols:
        recs.append(
            f"Consider trimming {', '.join(marginal_risk.disproportionate_symbols)} — "
            f"their risk contribution exceeds their capital weight."
        )

    if not recs:
        recs.append("No action required — continue monitoring.")

    explanation = (
        f"PORTFOLIO_RISK_STATUS: {status.value}\n\n"
        f"Primary Risk Drivers:\n" + "\n".join(f"- {d}" for d in drivers) + "\n\n"
        f"Recommended Action:\n" + "\n".join(f"- {r}" for r in recs)
    )

    return RecommendationResult(
        status=status,
        primary_drivers=drivers,
        recommendations=recs,
        explanation=explanation,
    )


def _fmt(value: float) -> str:
    if abs(value) < 10:
        return f"{value:.2%}"
    return f"{value:.2f}"
