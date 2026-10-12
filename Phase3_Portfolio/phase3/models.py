"""
Phase 3 integration data contract.

Two new input types, two new output types, nothing else:

  ExecutionInput   — the per-opportunity market/trade-plan facts that the
                     Portfolio Manager's PortfolioCandidate deliberately
                     does NOT carry (price, stop, ATR, lot size, traded
                     value, return history). Supplied by the caller from
                     Market Data / Feature Engine / Risk Model.

  Phase3Request    — portfolio state + candidates + context + the
                     ExecutionInput map, as one object.

  TradeInstruction — a funded, risk-cleared order intent, ready for the
                     Execution Engine. This is the ONLY thing downstream
                     of Phase 3 should act on.

  Phase3Result     — instructions + a per-candidate audit trail + the
                     before/after risk reports + the capital ledger.

Every field on TradeInstruction is either copied verbatim from an
upstream result or computed by the integration layer from two upstream
results; nothing here re-predicts return, cost, or risk.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .enums import AllocationStatus, Phase3Status, Stage


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ExecutionInput:
    """Per-opportunity execution facts, keyed by opportunity_id.

    The Portfolio Manager decides WHICH opportunities to take using
    scores alone; it never sees a price. Position Sizing, Capital
    Feasibility and Portfolio Risk all need prices, stops and liquidity.
    Rather than widen PortfolioCandidate (which would change an already
    validated contract), Phase 3 takes those facts here.
    """

    opportunity_id: str

    entry_price: Decimal
    # Reference entry price. Position value, cost, feasibility and risk
    # are all computed at this price — not at a price this layer invents.

    stop_loss: Optional[Decimal] = None
    # Absent stop => Position Sizing degrades to LEVEL_1 / rejects,
    # depending on its own config. Never synthesized here.

    target: Optional[Decimal] = None
    expected_exit_price: Optional[Decimal] = None

    atr: Optional[Decimal] = None
    atr_pct: Optional[Decimal] = None          # ATR / price, fractional
    volatility_annualized: Optional[float] = None   # for Portfolio Risk
    beta: Optional[float] = None

    average_traded_value: Optional[Decimal] = None  # currency / day
    average_volume: Optional[Decimal] = None        # shares / day

    lot_size: int = 1
    segment: str = "EQ"
    market_cap_class: Optional[str] = None

    return_history: List[float] = field(default_factory=list)
    # Daily fractional returns, most-recent-last. Drives correlation /
    # covariance / VaR in Portfolio Risk. Empty is allowed and degrades
    # those engines gracefully (they flag it), it is never faked.

    expected_cost_per_share: Optional[Decimal] = None
    # Optional override for what Position Sizing consumes as
    # `expected_total_cost` (documented in position_sizing/DESIGN.md as a
    # PER-SHARE round-trip estimate). If absent, Phase 3 derives it from
    # PortfolioCandidate.expected_total_cost according to
    # Phase3Config.candidate_cost_basis.

    same_day_cnc_square_off: bool = False


@dataclass(frozen=True)
class Phase3Request:
    portfolio_state: Any                      # portfolio_manager.PortfolioState
    candidates: List[Any]                     # List[portfolio_manager.PortfolioCandidate]
    market_context: Any                       # portfolio_manager.MarketContext
    execution_inputs: Dict[str, ExecutionInput]

    committed_capital: Optional[Decimal] = None
    # Capital already earmarked for orders placed earlier today that are
    # not yet reflected in PortfolioState.invested_capital. Unknown is
    # NOT the same as zero — see AccountCapitalState.committed_capital_is_known.

    equity_history: Optional[List[float]] = None   # for drawdown in Portfolio Risk
    open_position_inputs: Dict[str, ExecutionInput] = field(default_factory=dict)
    # Keyed by position_id (or symbol) — supplies return history /
    # volatility / traded value for ALREADY OPEN positions so the risk
    # model can see the real portfolio, not a history-less stub.


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TradeInstruction:
    """A funded, risk-cleared order intent. Consumed by the Execution Engine."""

    opportunity_id: str
    position_id: str
    symbol: str
    exchange: str
    sector: Optional[str]
    trade_type: str                 # INTRADAY | DELIVERY
    side: str                       # BUY (entry leg)

    quantity: int
    entry_price: Decimal
    position_value: Decimal
    estimated_entry_cost: Decimal   # buy-leg cost from the Cost Model
    required_capital: Decimal       # position_value + entry cost

    stop_loss: Optional[Decimal]
    target: Optional[Decimal]
    estimated_trade_risk: Decimal   # quantity * |entry - stop|, from Position Sizing

    sizing_status: str
    sizing_method: str
    sizing_confidence: str
    binding_constraints: List[str]

    feasibility_status: str
    max_affordable_quantity: int

    risk_score_before: Optional[float]
    risk_score_after: Optional[float]
    risk_status_after: Optional[str]
    risk_contribution_pct: Optional[float]

    allocation_status: AllocationStatus
    reduced_by_stage: Optional[Stage]
    explanation: str

    model_versions: Dict[str, str] = field(default_factory=dict)
    audit_ids: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CandidateOutcome:
    """One record per PM-selected candidate, allocated or not.

    `sizing_result`, `feasibility_result` and `impact` are the REAL
    upstream objects, not summaries — the integration layer never
    paraphrases a component's own verdict.
    """

    opportunity_id: str
    symbol: str
    status: AllocationStatus
    terminal_stage: Stage
    reason: str

    requested_quantity: int = 0
    final_quantity: int = 0

    sizing_result: Any = None            # position_sizing.PositionSizeResult
    feasibility_result: Any = None       # capital_feasibility.CapitalFeasibilityResult
    impact: Any = None                   # portfolio_risk.TradeImpactResult
    instruction: Optional[TradeInstruction] = None
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class CapitalSummary:
    total_capital: Decimal
    cash: Decimal
    reserve_required: Decimal
    deployable_at_start: Decimal
    capital_allocated: Decimal
    deployable_remaining: Decimal
    committed_capital_is_known: bool


@dataclass(frozen=True)
class Phase3Result:
    run_id: str
    timestamp: datetime
    portfolio_id: str
    status: Phase3Status

    instructions: List[TradeInstruction]
    outcomes: List[CandidateOutcome]

    portfolio_decision: Any = None       # portfolio_manager.PortfolioDecision
    existing_position_actions: List[Any] = field(default_factory=list)

    risk_before: Dict[str, Any] = field(default_factory=dict)
    risk_after: Dict[str, Any] = field(default_factory=dict)

    capital: Optional[CapitalSummary] = None

    warnings: List[str] = field(default_factory=list)
    explanation: str = ""
    component_versions: Dict[str, str] = field(default_factory=dict)

    @property
    def allocated_count(self) -> int:
        return len(self.instructions)

    def instruction_for(self, opportunity_id: str) -> Optional[TradeInstruction]:
        for i in self.instructions:
            if i.opportunity_id == opportunity_id:
                return i
        return None
