"""
Strongly typed I/O schemas for the Opportunity Scoring Model (spec §5, §62).

All monetary and percentage fields use Decimal for precision. All fields
are explicit about units in their docstring/description. Nothing here is
Optional unless the field is genuinely optional upstream (in which case
its absence is a `DataIncompleteError` at evaluation time, not a silent
default) - see exceptions.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field, field_validator, ConfigDict


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TradeType(str, Enum):
    DELIVERY = "DELIVERY"
    INTRADAY = "INTRADAY"
    SWING = "SWING"


class MarketRegimeLabel(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    PANIC = "PANIC"
    RECOVERY = "RECOVERY"


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    REJECTED = "REJECTED"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class RejectionReason(str, Enum):
    NET_RETURN_BELOW_MINIMUM = "NET_RETURN_BELOW_MINIMUM"
    PROBABILITY_OF_PROFIT_TOO_LOW = "PROBABILITY_OF_PROFIT_TOO_LOW"
    RISK_LIMIT_EXCEEDED = "RISK_LIMIT_EXCEEDED"
    STOP_LOSS_PROBABILITY_TOO_HIGH = "STOP_LOSS_PROBABILITY_TOO_HIGH"
    COST_ADJUSTED_EDGE_INSUFFICIENT = "COST_ADJUSTED_EDGE_INSUFFICIENT"
    BREAK_EVEN_MOVE_TOO_DEMANDING = "BREAK_EVEN_MOVE_TOO_DEMANDING"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    RELATIVE_ORDER_SIZE_TOO_LARGE = "RELATIVE_ORDER_SIZE_TOO_LARGE"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    PREDICTION_UNCERTAINTY_TOO_HIGH = "PREDICTION_UNCERTAINTY_TOO_HIGH"
    MODEL_CALIBRATION_TOO_POOR = "MODEL_CALIBRATION_TOO_POOR"
    CAPITAL_INFEASIBLE = "CAPITAL_INFEASIBLE"
    MARKET_REGIME_INCOMPATIBLE = "MARKET_REGIME_INCOMPATIBLE"
    HOLDING_PERIOD_TOO_LONG = "HOLDING_PERIOD_TOO_LONG"
    DATA_STALE = "DATA_STALE"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    ECONOMIC_VIABILITY_FAILED = "ECONOMIC_VIABILITY_FAILED"
    INVALID_INPUT = "INVALID_INPUT"


class ScoreConfidenceBand(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


# ---------------------------------------------------------------------------
# Upstream model output contracts (consumed, never recomputed)
# ---------------------------------------------------------------------------

class ReturnPrediction(BaseModel):
    """Owned by the Return Model. Units: percentages as e.g. 2.5 == 2.5%."""

    model_config = ConfigDict(frozen=True)

    expected_return_pct: Decimal
    expected_return_median_pct: Optional[Decimal] = None
    probability_of_profit: Decimal = Field(ge=0, le=1)
    probability_return_above_threshold: Optional[Dict[str, Decimal]] = None
    """e.g. {"0.0": 0.71, "1.0": 0.55, "2.0": 0.31} keyed by threshold_pct."""
    return_p10_pct: Optional[Decimal] = None
    return_p25_pct: Optional[Decimal] = None
    return_p75_pct: Optional[Decimal] = None
    return_p90_pct: Optional[Decimal] = None
    expected_shortfall_pct: Optional[Decimal] = None
    model_version: str
    generated_at: datetime

    @field_validator("probability_of_profit")
    @classmethod
    def _valid_prob(cls, v):
        if not (Decimal("0") <= v <= Decimal("1")):
            raise ValueError("probability_of_profit must be in [0,1]")
        return v


class RiskPrediction(BaseModel):
    """Owned by the Risk Model. Percentages as magnitudes (positive numbers)."""

    model_config = ConfigDict(frozen=True)

    expected_downside_pct: Decimal = Field(ge=0)
    expected_drawdown_pct: Optional[Decimal] = Field(default=None, ge=0)
    probability_stop_loss: Decimal = Field(ge=0, le=1)
    expected_max_adverse_excursion_pct: Decimal = Field(ge=0)
    expected_max_favourable_excursion_pct: Optional[Decimal] = Field(default=None, ge=0)
    volatility_pct: Optional[Decimal] = Field(default=None, ge=0)
    tail_risk_pct: Optional[Decimal] = Field(default=None, ge=0)
    """e.g. 99th percentile adverse move / expected shortfall magnitude."""
    model_version: str
    generated_at: datetime


class RegimePrediction(BaseModel):
    """Owned by the Market Regime Model."""

    model_config = ConfigDict(frozen=True)

    regime: MarketRegimeLabel
    regime_probability: Decimal = Field(ge=0, le=1)
    regime_compatibility: Decimal = Field(ge=0, le=1)
    """0-1: empirically-derived strategy/regime fit (spec §13). Owned by the
    Regime Model, not recomputed here."""
    strategy_regime_fit: Optional[Decimal] = Field(default=None, ge=0, le=1)
    model_version: str
    generated_at: datetime


class CostScenario(BaseModel):
    model_config = ConfigDict(frozen=True)
    expected_total_cost_pct: Decimal = Field(ge=0)
    expected_net_return_pct: Decimal
    cost_ratio: Decimal = Field(ge=0)
    """expected_total_cost_pct / |expected_gross_return_pct|, authoritative
    from the Cost Model. Not recalculated here (spec §9)."""
    break_even_move_pct: Decimal = Field(ge=0)
    economic_viability: bool


class CostPrediction(BaseModel):
    """Owned by the Cost Model - authoritative for all friction estimates."""

    model_config = ConfigDict(frozen=True)

    expected_gross_return_pct: Decimal
    base: CostScenario
    optimistic: Optional[CostScenario] = None
    conservative: Optional[CostScenario] = None
    model_version: str
    generated_at: datetime


class LiquidityInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    average_traded_value: Decimal = Field(gt=0, description="INR, trailing avg daily traded value")
    relative_order_size: Decimal = Field(ge=0, description="position_value / average_traded_value")
    spread_pct: Decimal = Field(ge=0)
    slippage_estimate_pct: Optional[Decimal] = Field(default=None, ge=0)
    """If provided by the Cost Model already, do not re-penalize (spec §14)."""


class PortfolioContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    available_capital: Decimal = Field(ge=0)
    current_exposure: Decimal = Field(ge=0)
    sector_exposure: Dict[str, Decimal] = Field(default_factory=dict)
    correlated_exposure: Optional[Decimal] = Field(default=None, ge=0)
    number_of_open_positions: int = Field(ge=0)


# ---------------------------------------------------------------------------
# The candidate trade itself
# ---------------------------------------------------------------------------

class OpportunityCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    exchange: str = "NSE"
    segment: str = "EQ"
    trade_type: TradeType
    strategy: str
    timestamp: datetime

    entry_price: Decimal = Field(gt=0)
    expected_exit_price: Decimal = Field(gt=0)
    position_size: int = Field(gt=0, description="number of shares/units")
    capital_required: Decimal = Field(gt=0, description="INR")

    holding_period_days: Decimal = Field(gt=0)

    prediction_confidence: Decimal = Field(ge=0, le=1)
    prediction_uncertainty: Decimal = Field(ge=0, le=1)
    model_calibration_quality: Decimal = Field(ge=0, le=1)

    return_prediction: ReturnPrediction
    risk_prediction: RiskPrediction
    regime_prediction: RegimePrediction
    cost_prediction: CostPrediction
    liquidity: LiquidityInfo
    portfolio_context: PortfolioContext

    data_generated_at: datetime = Field(
        description="Timestamp of the OLDEST upstream input snapshot used to "
        "build this candidate; used for staleness gating (spec §18, §45)."
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class ComponentScores(BaseModel):
    model_config = ConfigDict(frozen=True)

    net_edge: Decimal
    risk_efficiency: Decimal
    regime_compatibility: Decimal
    prediction_reliability: Decimal
    liquidity_execution: Decimal
    capital_efficiency: Decimal
    holding_time_efficiency: Decimal
    cost_resilience: Decimal
    tail_risk_multiplier: Decimal
    """Not a weighted component - a [0,1] multiplicative dampener."""


class ScenarioScores(BaseModel):
    model_config = ConfigDict(frozen=True)

    optimistic: Optional[Decimal] = None
    base: Decimal
    conservative: Optional[Decimal] = None
    scenario_resilience: Decimal = Field(description="0-1 multiplier")


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate_name: str
    passed: bool
    detail: str


class OpportunityAssessment(BaseModel):
    model_config = ConfigDict(frozen=True)

    opportunity_id: str
    symbol: str
    timestamp: datetime
    trade_type: TradeType
    strategy: str

    eligibility_status: EligibilityStatus

    opportunity_score: Optional[Decimal] = None
    score_band: Optional[str] = None
    score_confidence: Optional[ScoreConfidenceBand] = None
    score_uncertainty: Optional[Decimal] = None

    absolute_rank: Optional[int] = None
    relative_rank: Optional[int] = None

    expected_gross_return_pct: Optional[Decimal] = None
    expected_net_return_pct: Optional[Decimal] = None
    expected_net_profit: Optional[Decimal] = None
    probability_of_profit: Optional[Decimal] = None
    expected_downside_pct: Optional[Decimal] = None

    component_scores: Optional[ComponentScores] = None
    scenario_scores: Optional[ScenarioScores] = None

    capital_feasibility: bool
    economic_viability: Optional[bool] = None

    gate_results: List[GateResult] = Field(default_factory=list)
    rejection_reasons: List[RejectionReason] = Field(default_factory=list)
    ranking_explanation: Optional[str] = None

    model_version: str
    config_version: str
    input_versions: Dict[str, str] = Field(default_factory=dict)
    input_hash: Optional[str] = None

    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RankedOpportunitySet(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    total_candidates: int
    eligible_count: int
    rejected_count: int
    top_opportunities: List[OpportunityAssessment]
    all_assessments: List[OpportunityAssessment]
    no_trade: bool
    no_trade_reason: Optional[str] = None
