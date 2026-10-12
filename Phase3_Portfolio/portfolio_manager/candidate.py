"""PortfolioCandidate — external opportunity representation (spec §7)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from .enums import MarketRegime, Strategy, TradeType, ValidationState


@dataclass(frozen=True)
class PortfolioCandidate:
    # Identity
    opportunity_id: str
    symbol: str
    exchange: str

    # Classification
    trade_type: TradeType
    strategy: Strategy
    sector: str
    industry: Optional[str]

    # Opportunity / return model outputs
    opportunity_score: float
    predicted_return: float
    expected_net_return: float
    expected_net_profit: float
    probability_of_profit: float

    # Risk model outputs
    expected_risk: float
    expected_downside: float

    # Confidence / timing
    confidence: float
    expected_holding_period: float  # in days (fractional for intraday)

    # Cost model outputs
    economic_viability: bool
    expected_total_cost: float

    # Liquidity (from FILTER2.0 / liquidity qualification)
    liquidity_score: float

    # Regime
    regime: MarketRegime
    regime_compatibility: float  # 0..1, provided by Market Regime Model

    # Freshness
    signal_timestamp: datetime

    # Versioning (§45)
    model_versions: Dict[str, str] = field(default_factory=dict)

    def is_structurally_complete(self) -> bool:
        """Cheap structural check used by the validator before touching
        any numeric logic. Deep semantic checks live in validator.py."""
        required_non_null = [
            self.opportunity_id,
            self.symbol,
            self.exchange,
            self.sector,
            self.signal_timestamp,
        ]
        if any(v is None or v == "" for v in required_non_null):
            return False
        numeric_fields = [
            self.opportunity_score,
            self.predicted_return,
            self.expected_net_return,
            self.expected_net_profit,
            self.probability_of_profit,
            self.expected_risk,
            self.expected_downside,
            self.confidence,
            self.expected_holding_period,
            self.expected_total_cost,
            self.liquidity_score,
            self.regime_compatibility,
        ]
        return all(v is not None for v in numeric_fields)
