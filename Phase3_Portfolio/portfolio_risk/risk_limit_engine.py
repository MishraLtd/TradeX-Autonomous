"""
risk_limit_engine.py
-----------------------
Explicit risk-limit engine (spec section 9).

Evaluates every configured limit and returns PASS / WARNING / BREACH,
independent of the composite 0-100 score. This is the layer that produces
hard gating signals (e.g. for REJECT_NEW_POSITION decisions) even when the
composite score alone might look borderline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict

from .config import RiskLimits


class LimitStatus(str, Enum):
    PASS = "PASS"
    WARNING = "WARNING"
    BREACH = "BREACH"


@dataclass
class LimitCheck:
    value: float
    warn_threshold: float
    max_threshold: float
    status: LimitStatus
    higher_is_riskier: bool = True


@dataclass
class RiskLimitReport:
    checks: Dict[str, LimitCheck] = field(default_factory=dict)

    def breaches(self):
        return [k for k, v in self.checks.items() if v.status == LimitStatus.BREACH]

    def warnings(self):
        return [k for k, v in self.checks.items() if v.status == LimitStatus.WARNING]

    def worst_status(self) -> LimitStatus:
        if any(v.status == LimitStatus.BREACH for v in self.checks.values()):
            return LimitStatus.BREACH
        if any(v.status == LimitStatus.WARNING for v in self.checks.values()):
            return LimitStatus.WARNING
        return LimitStatus.PASS


def _check(value: float, warn: float, cap: float, higher_is_riskier: bool = True) -> LimitCheck:
    if higher_is_riskier:
        if value >= cap:
            status = LimitStatus.BREACH
        elif value >= warn:
            status = LimitStatus.WARNING
        else:
            status = LimitStatus.PASS
    else:
        # lower value is riskier (e.g. cash buffer)
        if value <= cap:
            status = LimitStatus.BREACH
        elif value <= warn:
            status = LimitStatus.WARNING
        else:
            status = LimitStatus.PASS
    return LimitCheck(value=value, warn_threshold=warn, max_threshold=cap, status=status,
                       higher_is_riskier=higher_is_riskier)


def evaluate_limits(
    limits: RiskLimits,
    largest_position_weight: float,
    largest_sector_weight: float,
    aggregate_stop_risk_pct: float,
    portfolio_volatility: float,
    var_95_pct: float,
    es_95_pct: float,
    num_positions: int,
    avg_correlation: float,
    max_pairwise_correlation: float,
    invested_capital_pct: float,
    cash_pct: float,
    current_drawdown: float,
    worst_stress_loss_pct: float,
    worst_liquidity_ratio: float,
) -> RiskLimitReport:
    checks = {
        "position_concentration": _check(largest_position_weight, limits.warn_position_weight, limits.max_position_weight),
        "sector_concentration": _check(largest_sector_weight, limits.warn_sector_weight, limits.max_sector_weight),
        "aggregate_stop_loss_risk": _check(aggregate_stop_risk_pct, limits.warn_aggregate_stop_risk_pct, limits.max_aggregate_stop_risk_pct),
        "portfolio_volatility": _check(portfolio_volatility, limits.warn_portfolio_volatility, limits.max_portfolio_volatility),
        "var_95": _check(var_95_pct, limits.warn_var_95_pct, limits.max_var_95_pct),
        "expected_shortfall_95": _check(es_95_pct, limits.warn_es_95_pct, limits.max_es_95_pct),
        "position_count": _check(num_positions, limits.warn_positions, limits.max_positions),
        "average_correlation": _check(avg_correlation, limits.warn_avg_correlation, limits.max_avg_correlation),
        "max_pairwise_correlation": _check(max_pairwise_correlation, limits.warn_pairwise_correlation, limits.max_pairwise_correlation),
        "invested_capital_pct": _check(invested_capital_pct, limits.warn_invested_capital_pct, limits.max_invested_capital_pct),
        "cash_buffer": _check(cash_pct, limits.min_cash_buffer_pct * 1.5, limits.min_cash_buffer_pct, higher_is_riskier=False),
        "drawdown": _check(current_drawdown, limits.warn_drawdown_pct, limits.max_drawdown_pct),
        "stress_loss": _check(worst_stress_loss_pct, limits.warn_stress_loss_pct, limits.max_stress_loss_pct),
        "liquidity_ratio": _check(worst_liquidity_ratio, limits.warn_liquidity_ratio, limits.max_liquidity_ratio),
    }
    return RiskLimitReport(checks=checks)
