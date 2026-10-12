"""PortfolioPosition / PortfolioState / MarketContext (spec §5-6)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .enums import (
    MarketRegime,
    PortfolioStatus,
    PositionStatus,
    Strategy,
    SystemStatus,
    TradeType,
)


@dataclass(frozen=True)
class PortfolioPosition:
    position_id: str
    symbol: str
    exchange: str
    trade_type: TradeType
    strategy: Strategy
    sector: str

    entry_price: float
    current_price: float
    quantity: int
    entry_time: datetime

    expected_return: float
    expected_remaining_return: float
    expected_risk: float

    opportunity_score: float
    confidence: float

    market_regime: MarketRegime
    model_version: str

    position_status: PositionStatus = PositionStatus.OPEN


@dataclass(frozen=True)
class MarketContext:
    """Point-in-time market context supplied by the caller (live or
    backtest). The Portfolio Manager NEVER reads wall-clock time itself —
    everything is judged relative to `as_of` (DESIGN PART 17, §36-37)."""

    as_of: datetime
    market_regime: MarketRegime
    regime_confidence: float


@dataclass(frozen=True)
class PortfolioState:
    portfolio_id: str
    timestamp: datetime

    # Authoritative capital figures — sourced externally; NOT computed here
    # (§5: "Do not implement detailed capital calculations here").
    total_equity: float
    cash: float
    invested_capital: float

    open_positions: List[PortfolioPosition] = field(default_factory=list)
    pending_allocations: List[str] = field(default_factory=list)
    pending_orders: List[str] = field(default_factory=list)

    daily_realized_pnl: float = 0.0
    daily_unrealized_pnl: float = 0.0

    portfolio_expected_return: Optional[float] = None
    portfolio_expected_risk: Optional[float] = None

    current_regime: MarketRegime = MarketRegime.UNKNOWN

    # Pre-aggregated exposure maps. Optional — engines will (re)compute from
    # open_positions if these are not supplied, but an authoritative
    # upstream value always takes precedence.
    strategy_exposure: Optional[Dict[str, float]] = None
    sector_exposure: Optional[Dict[str, float]] = None
    symbol_exposure: Optional[Dict[str, float]] = None
    correlation_exposure: Optional[Dict[str, float]] = None

    portfolio_position_count: Optional[int] = None
    portfolio_status: PortfolioStatus = PortfolioStatus.ACTIVE
    system_status: SystemStatus = SystemStatus.OK

    def is_valid(self) -> bool:
        if self.system_status == SystemStatus.UNAVAILABLE:
            return False
        if self.portfolio_status == PortfolioStatus.HALTED:
            return False
        if self.total_equity is None or self.total_equity < 0:
            return False
        return True

    def open_symbols(self) -> List[str]:
        return [p.symbol for p in self.open_positions]
