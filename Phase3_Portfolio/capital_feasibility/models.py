"""
PART — CAPITAL FEASIBILITY DATA CONTRACT

Decimal throughout for anything that touches money or ratios, matching
cost_model/models.py and position_sizing/models.py (the two components
this model integrates with most tightly numerically). portfolio_manager
uses float for PortfolioState; the adapter layer (adapters/portfolio_adapter.py)
is the single, documented place where that boundary is crossed.

Every dataclass here is frozen: the core model is pure/deterministic
(spec §25) and never mutates a caller's object.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from .enums import BlockingConstraint, FeasibilityGate, FeasibilityStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Capital state (spec §4)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AccountCapitalState:
    """Distinguishes total/invested/reserved/available rather than
    treating all equity as deployable (spec §4)."""

    total_capital: Decimal          # total account equity
    cash: Decimal                   # uninvested cash currently sitting in the account
    invested_capital: Decimal       # capital currently tied up in open positions
    committed_capital: Decimal = Decimal("0")
    # Capital already earmarked for pending orders/allocations that have
    # not yet settled into `invested_capital`. Optional — if the caller's
    # system does not track this, it defaults to 0 and that default is
    # recorded as an assumption in the result's warnings, never silently
    # treated as "no pending trades exist" with confidence.
    committed_capital_is_known: bool = True

    reserved_capital: Decimal = Decimal("0")
    # Filled in by capital_state.compute_reserved_capital(); present here
    # so the fully-populated state is a single immutable object.

    @property
    def uncommitted_capital(self) -> Decimal:
        """Cash minus capital already earmarked for other pending trades."""
        return self.cash - self.committed_capital

    @property
    def deployable_capital(self) -> Decimal:
        """The actual amount available for a NEW trade: cash, minus
        capital already committed to other pending trades, minus the
        configured reserve. Never negative in the arithmetic sense — a
        negative value here is meaningful (over-committed account) and is
        surfaced, not clamped, so callers can see how far over the line
        the account is."""
        return self.cash - self.committed_capital - self.reserved_capital

    @property
    def capital_utilization(self) -> Optional[Decimal]:
        if self.total_capital <= 0:
            return None
        return (self.invested_capital / self.total_capital).quantize(Decimal("0.000001"))


# --------------------------------------------------------------------------- #
# Portfolio exposure context (spec §9, §14)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AssetExposure:
    symbol: str
    existing_value: Decimal          # current market value of existing holdings in this symbol
    sector: Optional[str] = None
    existing_sector_value: Optional[Decimal] = None  # current market value across the whole sector


# --------------------------------------------------------------------------- #
# Trade / cost inputs (spec §5, §11, §26)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LiquidityContext:
    """Optional microstructure inputs (spec §11). Absence degrades Gate 6
    to a no-op (recorded as a warning), it never invents a number."""

    average_daily_traded_value: Optional[Decimal] = None
    average_daily_volume: Optional[int] = None


@dataclass(frozen=True)
class TradeCapitalRequest:
    """The single input to `evaluate()`. Deliberately consumes the
    Position Sizing result (spec §27) rather than re-deriving a size:
    `requested_quantity` is what Position Sizing recommended, not a
    number Capital Feasibility invents."""

    position_id: str
    opportunity_id: str
    symbol: str
    exchange: str            # "NSE" | "BSE" — passed through to the Cost Model
    sector: Optional[str]

    trade_type: str          # "INTRADAY" | "DELIVERY" — passed through to the Cost Model
    side: str = "BUY"        # capital feasibility is evaluated for the entry (opening) leg

    entry_price: Decimal = Decimal("0")
    requested_quantity: int = 0

    lot_size: int = 1

    liquidity: LiquidityContext = field(default_factory=LiquidityContext)

    same_day_cnc_square_off: bool = False

    timestamp: datetime = field(default_factory=_now)
    model_versions: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Per-gate diagnostics (spec §16, §20, §34)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class GateResult:
    gate: FeasibilityGate
    passed: bool
    constraint: BlockingConstraint
    detail: str
    # The maximum quantity this gate alone would permit, independent of
    # every other gate — used both for explainability and for the
    # max-feasible-quantity calculation (spec §10).
    max_quantity_allowed: Optional[int] = None


# --------------------------------------------------------------------------- #
# Trade capital requirement (spec §5, §12, §13)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TradeCapitalRequirement:
    quantity: int
    position_value: Decimal
    execution_cost: Decimal          # buy-leg cost from the Cost Model (statutory + slippage/spread/impact)
    required_capital: Decimal        # position_value + execution_cost
    cost_model_version: Optional[str] = None


# --------------------------------------------------------------------------- #
# Final decision output (spec §17)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class CapitalFeasibilityResult:
    position_id: str
    opportunity_id: str
    symbol: str

    capital_feasible: bool
    feasibility_status: FeasibilityStatus

    requested_quantity: int
    feasible_quantity: int
    maximum_feasible_quantity: int

    requested_position_value: Decimal
    feasible_position_value: Decimal

    required_capital: Decimal          # required capital AT feasible_quantity
    requested_required_capital: Decimal  # required capital AT requested_quantity (for comparison/explainability)
    available_capital: Decimal          # deployable_capital before the trade
    remaining_capital: Decimal          # deployable_capital after the trade at feasible_quantity

    capital_utilization_before: Optional[Decimal]
    capital_utilization_after: Optional[Decimal]

    capital_reserve_required: Decimal
    capital_reserve_remaining: Decimal   # cash - required_capital - reserve, at feasible_quantity

    post_trade_asset_exposure_ratio: Optional[Decimal]
    post_trade_sector_exposure_ratio: Optional[Decimal]

    gate_results: List[GateResult]
    blocking_constraints: List[BlockingConstraint]
    warnings: List[str]
    explanation: str

    # Feeds straight back into position_sizing.models.CapitalFeasibilityConstraint
    # (see adapters/position_sizing_adapter.py) so the loop can close.
    max_affordable_quantity: int
    max_affordable_value: Decimal

    model_versions: Dict[str, str]
    calculation_version: str
    calculation_timestamp: datetime
    audit_id: str
