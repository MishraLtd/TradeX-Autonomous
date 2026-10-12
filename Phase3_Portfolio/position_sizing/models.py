from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from .enums import (
    TradeType,
    MarketRegime,
    SizingStatus,
    SizingMethod,
    SizingLevel,
    SizingConfidence,
    BindingConstraint,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Upstream interfaces (Parts 16-18) — narrow, versioned, never duplicated.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CapitalFeasibilityConstraint:
    """Provided by the (future) Capital Feasibility component. Optional —
    if absent, Position Sizing falls back to request.capital_available_for_sizing
    and records that distinction in the audit trail."""
    max_affordable_value: Optional[Decimal] = None
    max_affordable_quantity: Optional[int] = None
    available_capital: Optional[Decimal] = None
    capital_constraint_note: Optional[str] = None


@dataclass(frozen=True)
class PortfolioRiskSizingConstraint:
    """Provided by the (future) Portfolio Risk component."""
    risk_multiplier: Optional[Decimal] = None
    max_position_risk: Optional[Decimal] = None
    halt_sizing: bool = False


@dataclass(frozen=True)
class PortfolioManagerInstruction:
    """Provided by the (already-built) Portfolio Manager."""
    portfolio_target_weight: Optional[Decimal] = None
    candidate_priority: Optional[int] = None
    preferred_exposure: Optional[Decimal] = None
    selection_status: str = "SELECTED"


@dataclass(frozen=True)
class LiquiditySizingConstraint:
    """Derived internally from request liquidity fields, or supplied
    externally if a richer liquidity model exists upstream."""
    max_participation_value: Optional[Decimal] = None
    liquidity_cap_quantity: Optional[int] = None
    note: Optional[str] = None


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PositionSizingRequest:
    position_id: str
    opportunity_id: str

    symbol: str
    exchange: str
    segment: str

    trade_type: TradeType
    strategy: str
    market_regime: MarketRegime

    entry_price: Decimal
    stop_loss: Optional[Decimal] = None
    target: Optional[Decimal] = None
    expected_exit_price: Optional[Decimal] = None

    predicted_return: Optional[Decimal] = None
    expected_net_return: Optional[Decimal] = None

    probability_of_profit: Optional[Decimal] = None
    probability_of_loss: Optional[Decimal] = None

    expected_upside: Optional[Decimal] = None     # currency per share
    expected_downside: Optional[Decimal] = None   # currency per share

    risk_score: Optional[Decimal] = None
    expected_risk: Optional[Decimal] = None

    volatility: Optional[Decimal] = None
    atr: Optional[Decimal] = None
    atr_pct: Optional[Decimal] = None

    model_confidence: Optional[Decimal] = None

    opportunity_score: Optional[Decimal] = None
    cost_adjusted_score: Optional[Decimal] = None

    # Documented assumption (DESIGN.md Part 4/12): this is the Cost Model's
    # estimated round-trip cost PER SHARE at a reference size — Position
    # Sizing does not recompute fees, it only consumes this number.
    expected_total_cost: Optional[Decimal] = None

    expected_holding_period: Optional[str] = None

    liquidity_score: Optional[Decimal] = None
    average_traded_value: Optional[Decimal] = None
    average_volume: Optional[Decimal] = None

    portfolio_priority: Optional[int] = None
    portfolio_requested_weight: Optional[Decimal] = None
    portfolio_target_exposure: Optional[Decimal] = None

    capital_available_for_sizing: Decimal = Decimal("0")
    max_position_value: Optional[Decimal] = None
    min_position_value: Optional[Decimal] = None

    max_risk_allowed: Optional[Decimal] = None
    max_quantity: Optional[int] = None
    min_quantity: Optional[int] = None

    allow_synthetic_stop_fallback: bool = False  # must be explicitly enabled (Part 46)

    timestamp: datetime = field(default_factory=_now)
    model_versions: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class PositionSizeResult:
    symbol: str

    recommended_quantity: int
    minimum_quantity: int
    maximum_quantity: int

    recommended_position_value: Decimal
    recommended_position_pct: Optional[Decimal]

    risk_per_share: Optional[Decimal]
    estimated_trade_risk: Decimal
    risk_pct: Optional[Decimal]

    expected_gross_profit: Optional[Decimal]
    expected_net_profit: Optional[Decimal]
    expected_net_return: Optional[Decimal]

    capital_efficiency: Optional[Decimal]
    risk_efficiency: Optional[Decimal]

    sizing_method: SizingMethod
    sizing_level: SizingLevel
    sizing_components: dict
    size_adjustments: dict
    binding_constraints: list

    sizing_status: SizingStatus
    rejection_reason: Optional[str]

    confidence_adjustment: Optional[Decimal]
    volatility_adjustment: Optional[Decimal]
    liquidity_adjustment: Optional[Decimal]
    regime_adjustment: Optional[Decimal]
    portfolio_adjustment: Optional[Decimal]

    sizing_confidence: SizingConfidence

    model_versions: dict
    calculation_timestamp: datetime
    audit_id: str
