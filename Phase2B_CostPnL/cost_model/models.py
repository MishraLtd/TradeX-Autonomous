"""
PART 5 — COST MODEL DATA CONTRACT

Strongly-typed input/output schema for the Cost Model. Decimal is used
throughout for anything that touches money or percentages, never float,
so that rounding behaviour is deterministic and auditable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any


# --------------------------------------------------------------------------
# Enums (closed vocabularies — an unknown value must raise, never silently
# fall back to a default segment/product).
# --------------------------------------------------------------------------

class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class Segment(str, Enum):
    EQUITY = "EQUITY"          # equity cash market (delivery or intraday)
    # F&O / currency / commodity intentionally out of scope for this
    # ₹1,000-capital build — PART 24 (do not overengineer). Adding a new
    # segment means adding a new fee schedule row, not touching calculators.


class ProductType(str, Enum):
    CNC = "CNC"     # Cash & Carry — delivery
    MIS = "MIS"     # Margin Intraday Square-off
    # NOTE: PART 3 — a CNC order that is bought and sold within the same
    # session is NOT automatically re-priced as intraday by this model.
    # Whether that happens is a broker back-office decision (Zerodha does
    # not charge intraday brokerage on a same-day CNC buy+sell — it still
    # bills it as two delivery legs, but STT is the FULL delivery rate on
    # BOTH legs, which is often *worse* than MIS). This model exposes an
    # explicit `same_day_cnc_square_off` flag on TradeInput so the caller
    # states the fact rather than the model inferring it silently.


class TradeType(str, Enum):
    DELIVERY = "DELIVERY"
    INTRADAY = "INTRADAY"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Scenario(str, Enum):
    OPTIMISTIC = "OPTIMISTIC"
    BASE = "BASE"
    CONSERVATIVE = "CONSERVATIVE"


class EconomicViability(str, Enum):
    PASS = "PASS"
    REJECT = "REJECT"


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------

@dataclass
class LiquiditySnapshot:
    """Optional market microstructure context. All fields optional —
    absence degrades the slippage/impact model to a lower, more
    conservative tier rather than raising, UNLESS the trade is large
    enough relative to capital that the degraded tier itself becomes
    unsafe (see slippage.py)."""
    best_bid: Optional[Decimal] = None
    best_ask: Optional[Decimal] = None
    avg_daily_traded_value: Optional[Decimal] = None   # in ₹, trailing N-day avg
    avg_daily_volume: Optional[int] = None
    recent_volatility_pct: Optional[Decimal] = None    # e.g. ATR% or stdev of returns
    time_of_day: Optional[str] = None                  # "OPEN" | "MID" | "CLOSE"


@dataclass
class TradeInput:
    # identity
    symbol: str
    exchange: Exchange
    segment: Segment
    product_type: ProductType
    trade_type: TradeType

    # order facts
    side: Side
    quantity: int
    entry_price: Decimal
    order_type: OrderType = OrderType.MARKET

    exit_price: Optional[Decimal] = None
    entry_timestamp: Optional[datetime] = None
    exit_timestamp: Optional[datetime] = None

    # execution facts (filled in by the execution layer for POST-TRADE
    # costing; optional for PRE-TRADE estimation)
    execution_price: Optional[Decimal] = None
    filled_quantity: Optional[int] = None

    # capital context
    available_capital: Optional[Decimal] = None
    portfolio_value: Optional[Decimal] = None

    # liquidity / microstructure
    liquidity: LiquiditySnapshot = field(default_factory=LiquiditySnapshot)

    # explicit disambiguation flags (PART 3)
    same_day_cnc_square_off: bool = False

    # DP charge applicability — a delivery SELL out of existing demat
    # holdings attracts a DP charge; a same-day buy+sell of a CNC order
    # that never actually settles into the demat account does NOT.
    shares_from_demat_holdings: Optional[bool] = None

    def __post_init__(self):
        self.entry_price = Decimal(str(self.entry_price))
        if self.exit_price is not None:
            self.exit_price = Decimal(str(self.exit_price))
        if self.execution_price is not None:
            self.execution_price = Decimal(str(self.execution_price))
        if self.available_capital is not None:
            self.available_capital = Decimal(str(self.available_capital))
        if self.portfolio_value is not None:
            self.portfolio_value = Decimal(str(self.portfolio_value))

    @property
    def turnover(self) -> Decimal:
        """Order-level turnover in ₹ at the reference (entry) price."""
        return (self.entry_price * self.quantity).quantize(Decimal("0.01"))

    @property
    def position_value(self) -> Decimal:
        return self.turnover


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

@dataclass
class CostComponent:
    """A single named, sourced, auditable cost line item."""
    name: str
    amount: Decimal
    rule: str            # human-readable calculation rule actually applied
    source: str           # short source label, e.g. "Zerodha charges page"
    source_url: str
    side: Optional[Side] = None


@dataclass
class CostBreakdown:
    trade: TradeInput
    scenario: Scenario

    gross_pnl: Decimal

    brokerage: Decimal
    stt: Decimal
    exchange_charges: Decimal
    ipft_charges: Decimal
    sebi_charges: Decimal
    gst: Decimal
    stamp_duty: Decimal
    dp_charges: Decimal

    slippage_cost: Decimal
    spread_cost: Decimal
    market_impact_cost: Decimal

    total_statutory_broker_cost: Decimal   # brokerage+stt+exch+ipft+sebi+gst+stamp+dp
    total_execution_cost: Decimal          # slippage+spread+impact
    total_cost: Decimal

    net_pnl: Decimal
    gross_return_pct: Decimal
    net_return_pct: Decimal

    cost_as_pct_of_capital: Optional[Decimal]
    cost_as_pct_of_position: Decimal
    cost_as_pct_of_gross_profit: Optional[Decimal]

    break_even_move_pct: Decimal
    break_even_price: Decimal

    minimum_expected_return_required_pct: Decimal
    economic_viability: EconomicViability
    rejection_reasons: List[str]

    components: List[CostComponent]
    audit_trail: List[str]
    fee_schedule_version: str

    def as_report_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.trade.symbol,
            "trade_type": self.trade.trade_type.value,
            "scenario": self.scenario.value,
            "entry": str(self.trade.entry_price),
            "exit": str(self.trade.exit_price) if self.trade.exit_price else None,
            "quantity": self.trade.quantity,
            "capital_used": str(self.trade.turnover),
            "gross_pnl": str(self.gross_pnl),
            "brokerage": str(self.brokerage),
            "stt": str(self.stt),
            "exchange_charges": str(self.exchange_charges),
            "ipft_charges": str(self.ipft_charges),
            "sebi_charges": str(self.sebi_charges),
            "gst": str(self.gst),
            "stamp_duty": str(self.stamp_duty),
            "dp_charges": str(self.dp_charges),
            "slippage_cost": str(self.slippage_cost),
            "spread_cost": str(self.spread_cost),
            "market_impact_cost": str(self.market_impact_cost),
            "total_cost": str(self.total_cost),
            "net_pnl": str(self.net_pnl),
            "net_return_pct": str(self.net_return_pct),
            "break_even_move_pct": str(self.break_even_move_pct),
            "cost_as_pct_of_capital": (
                str(self.cost_as_pct_of_capital)
                if self.cost_as_pct_of_capital is not None else None
            ),
            "cost_as_pct_of_gross_profit": (
                str(self.cost_as_pct_of_gross_profit)
                if self.cost_as_pct_of_gross_profit is not None else None
            ),
            "economic_viability": self.economic_viability.value,
            "rejection_reasons": self.rejection_reasons,
        }
