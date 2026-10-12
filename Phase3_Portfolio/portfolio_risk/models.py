"""
models.py
---------
Typed data structures shared across the Portfolio Risk Model.

These intentionally mirror the fields listed in the project spec (section 4)
and are meant to be constructed from whatever the upstream TradeX modules
(Portfolio Manager, Position Sizing, Capital Feasibility, Risk/Return
Prediction Models, Market Regime Model, FILTER2.0) already produce.

Nothing here recomputes an upstream metric; it only carries it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional
import math


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class DataQualityState(str, Enum):
    VALID = "VALID"
    LIMITED_DATA = "LIMITED_DATA"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass
class Position:
    """A single open or proposed position."""

    symbol: str
    quantity: float
    entry_price: float
    current_price: float
    sector: Optional[str] = None
    market_cap_class: Optional[str] = None

    stop_loss_price: Optional[float] = None
    direction: Direction = Direction.LONG

    # Upstream-provided metrics (consumed, not recomputed)
    expected_return: Optional[float] = None       # fractional, e.g. 0.05 = 5%
    predicted_probability: Optional[float] = None
    confidence_score: Optional[float] = None
    predicted_risk: Optional[float] = None         # from Risk Prediction Model
    expected_net_pnl: Optional[float] = None       # after Cost Model
    transaction_cost: Optional[float] = None

    volatility: Optional[float] = None             # annualized, fractional
    atr: Optional[float] = None
    beta: Optional[float] = None

    avg_traded_value: Optional[float] = None       # liquidity proxy (currency/day)
    trade_horizon_days: Optional[int] = None

    is_proposed: bool = False

    # Historical daily returns for this symbol, most-recent-last.
    # Optional: if absent, correlation/covariance/VaR degrade gracefully.
    return_history: List[float] = field(default_factory=list)

    realized_pnl: float = 0.0

    # ---- Derived, read-only convenience properties -----------------
    @property
    def market_value(self) -> float:
        if self.quantity is None or self.current_price is None:
            return 0.0
        return self.quantity * self.current_price

    @property
    def capital_invested(self) -> float:
        if self.quantity is None or self.entry_price is None:
            return 0.0
        return self.quantity * self.entry_price

    @property
    def unrealized_pnl(self) -> float:
        if self.quantity is None or self.entry_price is None or self.current_price is None:
            return 0.0
        sign = 1 if self.direction == Direction.LONG else -1
        return sign * self.quantity * (self.current_price - self.entry_price)

    @property
    def stop_loss_distance(self) -> Optional[float]:
        if self.stop_loss_price is None or self.entry_price is None:
            return None
        return abs(self.entry_price - self.stop_loss_price)

    @property
    def stop_loss_risk_amount(self) -> Optional[float]:
        """Quantity_i * |Entry_i - Stop_i| — per spec section 5.K."""
        d = self.stop_loss_distance
        if d is None or self.quantity is None:
            return None
        return abs(self.quantity) * d

    def is_valid(self) -> bool:
        if self.quantity is None or self.quantity == 0:
            return False
        if self.entry_price is None or self.entry_price <= 0:
            return False
        if self.current_price is None or self.current_price <= 0:
            return False
        if math.isnan(self.quantity) or math.isnan(self.entry_price) or math.isnan(self.current_price):
            return False
        return True


@dataclass
class PortfolioState:
    """Portfolio-level snapshot, independent of individual positions' detail."""

    total_capital: float
    available_cash: float
    positions: List[Position] = field(default_factory=list)

    peak_portfolio_value: Optional[float] = None
    current_portfolio_value: Optional[float] = None

    daily_pnl: Optional[float] = None
    cumulative_pnl: Optional[float] = None

    market_regime: str = "UNKNOWN"

    # Optional pre-supplied covariance/correlation matrix (symbol -> symbol -> value).
    # If absent, computed from return_history where possible.
    covariance_matrix: Optional[Dict[str, Dict[str, float]]] = None

    def open_positions(self) -> List[Position]:
        return [p for p in self.positions if not p.is_proposed]

    def proposed_positions(self) -> List[Position]:
        return [p for p in self.positions if p.is_proposed]

    def invested_capital(self) -> float:
        return sum(p.market_value for p in self.open_positions())

    def portfolio_equity(self) -> float:
        """Cash + market value of open positions."""
        return self.available_cash + self.invested_capital()

    def current_drawdown(self) -> Optional[float]:
        pv = self.current_portfolio_value
        if pv is None:
            pv = self.portfolio_equity()
        if not self.peak_portfolio_value or self.peak_portfolio_value <= 0:
            return None
        dd = (self.peak_portfolio_value - pv) / self.peak_portfolio_value
        return max(0.0, dd)


@dataclass
class DataQualityReport:
    status: DataQualityState = DataQualityState.VALID
    notes: List[str] = field(default_factory=list)

    def downgrade(self, new_status: DataQualityState, note: str) -> None:
        order = [
            DataQualityState.VALID,
            DataQualityState.LIMITED_DATA,
            DataQualityState.INSUFFICIENT_HISTORY,
            DataQualityState.DEGRADED,
            DataQualityState.UNAVAILABLE,
        ]
        if order.index(new_status) > order.index(self.status):
            self.status = new_status
        self.notes.append(note)
