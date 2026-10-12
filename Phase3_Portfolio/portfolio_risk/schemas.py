"""
schemas.py
------------
Documents the machine-readable output contract of
`PortfolioRiskEngine.assess()` (spec section 19). These TypedDicts are for
static-typing / documentation purposes — the engine returns plain dicts
matching this shape, so downstream consumers (Portfolio Manager, Position
Sizing) do not need to import this package's internal dataclasses.

This is the authoritative reference for the Portfolio -> Risk Model and
Risk Model -> Portfolio Manager / Position Sizing contract (spec section 27).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple, TypedDict


class CapitalSchema(TypedDict):
    total_capital: float
    invested_capital: float
    available_cash: float
    capital_utilization: float
    capital_tier: str


class ConcentrationSchema(TypedDict):
    largest_position_weight: float
    largest_position_symbol: str
    top2_weight: float
    top5_weight: float
    hhi: float
    score: float


class SectorSchema(TypedDict):
    capital_by_sector: Dict[str, float]
    weight_by_sector: Dict[str, float]
    largest_sector: str
    largest_sector_weight: float
    sector_hhi: float
    score: float


class CorrelationSchema(TypedDict):
    average_correlation: float
    maximum_correlation: float
    max_pair: Tuple[str, str]
    high_correlation_pairs: List[Tuple[str, str, float]]
    symbols_excluded: List[str]
    score: float


class VolatilitySchema(TypedDict):
    portfolio_volatility_daily: float
    portfolio_volatility_annualized: float
    method: str


class TailRiskSchema(TypedDict):
    var: Dict[str, float]
    var_parametric: Dict[str, float]
    var_method: str
    var_95: float
    expected_shortfall: Dict[str, float]
    expected_shortfall_95: float


class DrawdownSchema(TypedDict):
    current_drawdown: Optional[float]
    max_drawdown: Optional[float]
    rolling_drawdown: Optional[float]
    state: str


class StopLossSchema(TypedDict):
    per_position_risk: Dict[str, float]
    positions_missing_stop: List[str]
    aggregate_risk_amount: float
    aggregate_risk_pct_of_capital: float
    largest_single_symbol: str
    correlated_cluster_risk_pct: float


class PortfolioRiskReport(TypedDict):
    portfolio_risk_score: float
    risk_class: str          # LOW | MODERATE | HIGH | SEVERE | CRITICAL
    risk_status: str         # ACCEPT | ACCEPT_WITH_WARNING | REDUCE_EXPOSURE |
                              # REJECT_NEW_POSITION | EMERGENCY_REDUCTION
    capital: CapitalSchema
    concentration: ConcentrationSchema
    sector: SectorSchema
    correlation: CorrelationSchema
    volatility: VolatilitySchema
    downside_risk: dict
    tail_risk: TailRiskSchema
    drawdown: DrawdownSchema
    stop_loss: StopLossSchema
    liquidity: dict
    stress_tests: Dict[str, float]
    worst_stress_scenario: str
    marginal_risk: dict
    risk_budget: dict
    risk_limits: Dict[str, str]           # category -> PASS | WARNING | BREACH
    risk_limit_detail: dict
    risk_drivers: List[str]
    recommendations: List[str]
    decision_explanation: str
    sub_scores: Dict[str, float]
    classification_notes: List[str]
    data_quality: dict
