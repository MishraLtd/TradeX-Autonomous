"""
Type bridges between the four components.

Each component was specified with its own numeric convention:

  portfolio_manager  float
  position_sizing    Decimal
  capital_feasibility Decimal
  portfolio_risk     float

Every crossing of that boundary happens here (or in the adapters
capital_feasibility already ships), always via Decimal(str(x)) / float(x)
so binary float noise never leaks into money arithmetic. No module in
`phase3` outside this one converts a numeric type.

Nothing here computes a new financial quantity. It only reshapes values
that a component already produced.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from capital_feasibility.models import AssetExposure, LiquidityContext, TradeCapitalRequest
from portfolio_risk.models import Direction as RiskDirection
from portfolio_risk.models import PortfolioState as RiskPortfolioState
from portfolio_risk.models import Position as RiskPosition
from position_sizing.enums import TradeType as SizingTradeType
from position_sizing.models import (
    PortfolioManagerInstruction,
    PortfolioRiskSizingConstraint,
    PositionSizingRequest,
)

from .config import Phase3Config
from .models import ExecutionInput
from .regime import to_risk_regime, to_sizing_regime


def d(x) -> Decimal:
    """float/int/str -> Decimal without importing binary float noise."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def f(x) -> float:
    return float(x)


# --------------------------------------------------------------------------- #
# Portfolio Manager + ExecutionInput -> Position Sizing request
# --------------------------------------------------------------------------- #

def _cost_per_share(candidate, exec_input: ExecutionInput, config: Phase3Config) -> Optional[Decimal]:
    """Position Sizing consumes a PER-SHARE round-trip cost estimate
    (position_sizing/DESIGN.md Part 4/12). PortfolioCandidate carries
    `expected_total_cost` whose basis depends on how the Cost Model was
    invoked upstream — so the basis is declared in config, and the
    PER_TRADE case refuses to guess a reference size rather than
    manufacturing a per-share number."""
    if exec_input.expected_cost_per_share is not None:
        return exec_input.expected_cost_per_share
    if candidate.expected_total_cost is None:
        return None
    if config.candidate_cost_basis == "PER_SHARE":
        return d(candidate.expected_total_cost)
    return None


def sizing_request_from_candidate(
    candidate,
    exec_input: ExecutionInput,
    position_id: str,
    capital_available: Decimal,
    market_context,
    config: Phase3Config,
    max_position_value: Optional[Decimal] = None,
    portfolio_priority: Optional[int] = None,
) -> PositionSizingRequest:
    return PositionSizingRequest(
        position_id=position_id,
        opportunity_id=candidate.opportunity_id,
        symbol=candidate.symbol,
        exchange=candidate.exchange,
        segment=exec_input.segment,
        trade_type=SizingTradeType(candidate.trade_type.value),
        strategy=candidate.strategy.value,
        market_regime=to_sizing_regime(
            market_context.market_regime, market_context.regime_confidence, config
        ),
        entry_price=exec_input.entry_price,
        stop_loss=exec_input.stop_loss,
        target=exec_input.target,
        expected_exit_price=exec_input.expected_exit_price,
        predicted_return=d(candidate.predicted_return),
        expected_net_return=d(candidate.expected_net_return),
        probability_of_profit=d(candidate.probability_of_profit),
        # Complement taken in Decimal, after the crossing: 1.0 - 0.58 in
        # binary float is 0.42000000000000004, and that noise would ride
        # all the way into the Kelly/EV comparison sizes.
        probability_of_loss=Decimal("1") - d(candidate.probability_of_profit)
        if candidate.probability_of_profit is not None
        else None,
        # Per-SHARE currency amounts (position_sizing/models.py). They are
        # derived from the trade plan's own prices, never from the
        # candidate's whole-trade `expected_net_profit` / fractional
        # `expected_downside`, whose per-share basis is not knowable here.
        expected_upside=(
            exec_input.target - exec_input.entry_price if exec_input.target is not None else None
        ),
        expected_downside=(
            exec_input.entry_price - exec_input.stop_loss
            if exec_input.stop_loss is not None
            else None
        ),
        risk_score=None,
        expected_risk=d(candidate.expected_risk) if candidate.expected_risk is not None else None,
        volatility=d(exec_input.volatility_annualized)
        if exec_input.volatility_annualized is not None
        else None,
        atr=exec_input.atr,
        atr_pct=exec_input.atr_pct,
        model_confidence=d(candidate.confidence),
        opportunity_score=d(candidate.opportunity_score),
        cost_adjusted_score=None,
        expected_total_cost=_cost_per_share(candidate, exec_input, config),
        expected_holding_period=str(candidate.expected_holding_period),
        liquidity_score=d(candidate.liquidity_score),
        average_traded_value=exec_input.average_traded_value,
        average_volume=exec_input.average_volume,
        portfolio_priority=portfolio_priority,
        capital_available_for_sizing=capital_available,
        max_position_value=max_position_value,
        timestamp=market_context.as_of,
        model_versions=dict(candidate.model_versions),
    )


def pm_instruction(priority: Optional[int]) -> PortfolioManagerInstruction:
    """The PM's own verdict, passed to Position Sizing as an instruction
    rather than being re-derived from scores here."""
    return PortfolioManagerInstruction(
        candidate_priority=priority,
        selection_status="SELECTED",
    )


def risk_sizing_constraint(risk_report: Dict, config: Phase3Config) -> PortfolioRiskSizingConstraint:
    """Portfolio Risk report -> Position Sizing constraint.

    `halt_sizing` is set from the risk model's own actionable gate, never
    from the score: the score is a continuous diagnostic, risk_status is
    the decision the risk model actually makes (portfolio_risk/docs/
    INTEGRATION.md section 3).
    """
    status = risk_report.get("risk_status", "ACCEPT")
    multiplier = config.risk_status_multipliers.get(status, Decimal("0.50"))
    return PortfolioRiskSizingConstraint(
        risk_multiplier=multiplier,
        max_position_risk=None,
        halt_sizing=status in config.halt_risk_statuses,
    )


def risk_position_cap(risk_portfolio: RiskPortfolioState, config: Phase3Config) -> Optional[Decimal]:
    """MAX_ACCEPTABLE_POSITION_SIZE, derived exactly as
    portfolio_risk/docs/INTEGRATION.md section 4a prescribes: the
    regime-adjusted concentration limit applied to portfolio equity."""
    if not config.apply_risk_position_cap:
        return None
    limits = config.risk.limits_for_regime(risk_portfolio.market_regime)
    equity = risk_portfolio.portfolio_equity()
    if equity <= 0:
        return None
    return d(limits.max_position_weight * equity)


# --------------------------------------------------------------------------- #
# Position Sizing -> Capital Feasibility
# --------------------------------------------------------------------------- #

def trade_capital_request(
    sizing_request: PositionSizingRequest,
    quantity: int,
    sector: Optional[str],
    exec_input: ExecutionInput,
) -> TradeCapitalRequest:
    """Same shape capital_feasibility's own position_sizing adapter
    builds, but with an explicit quantity so the post-risk downsize loop
    can re-evaluate a reduced size without fabricating a fake
    PositionSizeResult."""
    return TradeCapitalRequest(
        position_id=sizing_request.position_id,
        opportunity_id=sizing_request.opportunity_id,
        symbol=sizing_request.symbol,
        exchange=sizing_request.exchange,
        sector=sector,
        trade_type=sizing_request.trade_type.value,
        side="BUY",
        entry_price=sizing_request.entry_price,
        requested_quantity=max(0, quantity),
        lot_size=exec_input.lot_size,
        liquidity=LiquidityContext(
            average_daily_traded_value=exec_input.average_traded_value,
            average_daily_volume=(
                int(exec_input.average_volume) if exec_input.average_volume is not None else None
            ),
        ),
        same_day_cnc_square_off=exec_input.same_day_cnc_square_off,
        timestamp=sizing_request.timestamp,
        model_versions=dict(sizing_request.model_versions),
    )


def asset_exposure(
    portfolio_state,
    symbol: str,
    sector: Optional[str],
    extra_symbol_value: Decimal = Decimal("0"),
    extra_sector_value: Decimal = Decimal("0"),
) -> AssetExposure:
    """Exposure at market value, including trades already allocated
    EARLIER IN THIS SAME RUN.

    capital_feasibility's shipped portfolio adapter only sees
    PortfolioState.open_positions, which is correct for a single trade
    evaluated in isolation. Within one Phase 3 batch, however, the second
    candidate must see the first candidate's allocation — otherwise five
    candidates in one sector each pass a 60% sector gate independently
    and the portfolio ends up 3x over the limit.
    """
    existing = Decimal("0")
    sector_value: Optional[Decimal] = Decimal("0") if sector is not None else None

    for pos in portfolio_state.open_positions:
        market_value = d(pos.current_price) * pos.quantity
        if pos.symbol == symbol:
            existing += market_value
        if sector is not None and pos.sector == sector:
            sector_value += market_value

    existing += extra_symbol_value
    if sector_value is not None:
        sector_value += extra_sector_value

    return AssetExposure(
        symbol=symbol,
        existing_value=existing,
        sector=sector,
        existing_sector_value=sector_value,
    )


# --------------------------------------------------------------------------- #
# Portfolio Manager / Position Sizing -> Portfolio Risk
# --------------------------------------------------------------------------- #

def risk_position_from_open(position, exec_input: Optional[ExecutionInput]) -> RiskPosition:
    """An already-open PM position, expressed for the risk model.

    Where the caller supplied an ExecutionInput for the position, its
    history/volatility/liquidity are attached; where they did not, the
    fields stay None and the risk engines flag the degradation
    themselves. Nothing is imputed here.
    """
    ei = exec_input
    return RiskPosition(
        symbol=position.symbol,
        quantity=float(position.quantity),
        entry_price=f(position.entry_price),
        current_price=f(position.current_price),
        sector=position.sector,
        market_cap_class=ei.market_cap_class if ei else None,
        stop_loss_price=f(ei.stop_loss) if ei and ei.stop_loss is not None else None,
        direction=RiskDirection.LONG,
        expected_return=f(position.expected_remaining_return)
        if position.expected_remaining_return is not None
        else None,
        confidence_score=f(position.confidence) if position.confidence is not None else None,
        predicted_risk=f(position.expected_risk) if position.expected_risk is not None else None,
        volatility=ei.volatility_annualized if ei else None,
        atr=f(ei.atr) if ei and ei.atr is not None else None,
        beta=ei.beta if ei else None,
        avg_traded_value=f(ei.average_traded_value)
        if ei and ei.average_traded_value is not None
        else None,
        return_history=list(ei.return_history) if ei else [],
        is_proposed=False,
    )


def proposed_risk_position(
    candidate,
    exec_input: ExecutionInput,
    quantity: int,
    expected_net_pnl: Optional[Decimal] = None,
    transaction_cost: Optional[Decimal] = None,
) -> RiskPosition:
    """A trade Phase 3 is considering, expressed for the risk model.

    entry_price and current_price are both the reference entry price:
    the trade has not happened, so there is no mark yet, and marking it
    at anything else would put a fictional unrealized P&L into the
    portfolio the risk model sees.
    """
    price = f(exec_input.entry_price)
    return RiskPosition(
        symbol=candidate.symbol,
        quantity=float(quantity),
        entry_price=price,
        current_price=price,
        sector=candidate.sector,
        market_cap_class=exec_input.market_cap_class,
        stop_loss_price=f(exec_input.stop_loss) if exec_input.stop_loss is not None else None,
        direction=RiskDirection.LONG,
        expected_return=f(candidate.expected_net_return),
        predicted_probability=f(candidate.probability_of_profit),
        confidence_score=f(candidate.confidence),
        predicted_risk=f(candidate.expected_risk),
        expected_net_pnl=f(expected_net_pnl) if expected_net_pnl is not None else None,
        transaction_cost=f(transaction_cost) if transaction_cost is not None else None,
        volatility=exec_input.volatility_annualized,
        atr=f(exec_input.atr) if exec_input.atr is not None else None,
        beta=exec_input.beta,
        avg_traded_value=f(exec_input.average_traded_value)
        if exec_input.average_traded_value is not None
        else None,
        trade_horizon_days=int(candidate.expected_holding_period)
        if candidate.expected_holding_period is not None
        else None,
        return_history=list(exec_input.return_history),
        is_proposed=True,
    )


def risk_portfolio_from_pm(
    portfolio_state,
    open_position_inputs: Dict[str, ExecutionInput],
    config: Phase3Config,
    extra_positions: Optional[List[RiskPosition]] = None,
    available_cash_override: Optional[float] = None,
) -> RiskPortfolioState:
    """PM PortfolioState -> risk PortfolioState.

    `open_position_inputs` may be keyed by position_id or by symbol;
    position_id wins when both are present.
    """
    positions: List[RiskPosition] = []
    for pos in portfolio_state.open_positions:
        ei = open_position_inputs.get(pos.position_id) or open_position_inputs.get(pos.symbol)
        positions.append(risk_position_from_open(pos, ei))

    if extra_positions:
        positions.extend(extra_positions)

    cash = (
        available_cash_override
        if available_cash_override is not None
        else f(portfolio_state.cash)
    )

    return RiskPortfolioState(
        total_capital=f(portfolio_state.total_equity),
        available_cash=cash,
        positions=positions,
        current_portfolio_value=f(portfolio_state.total_equity),
        daily_pnl=f(portfolio_state.daily_realized_pnl + portfolio_state.daily_unrealized_pnl),
        market_regime=to_risk_regime(portfolio_state.current_regime, config),
    )
