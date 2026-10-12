"""
PART — CAPITAL CONSTRAINT HIERARCHY (spec §16)

Each gate is a pure function: (candidate requirement + state) -> GateResult.
`max_quantity.py` calls `evaluate_all()` at successive candidate
quantities during its search; `engine.py` also calls it once at the
originally REQUESTED quantity purely for explainability (to report which
gate(s) blocked the request as asked, even though the final decision is
driven by the search in max_quantity.py).

Gate order matches spec §16 exactly:
  1. Basic Capital Availability
  2. Capital Reserve
  3. Position Allocation (this-trade-alone + cumulative asset exposure)
  4. Portfolio Exposure (sector concentration + aggregate portfolio cap)
  5. Execution Capital (full transaction affordable after costs, at the
     quantity actually being evaluated — a re-check, since costs are not
     strictly linear in quantity)
  6. Liquidity / Execution Constraint
Gate 7 (Final Feasibility) is not a check here — it is the decision
engine.py builds by combining gates 1-6 with the max-quantity search.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import List, Optional

from .config import CapitalFeasibilityConfig
from .enums import BlockingConstraint, FeasibilityGate
from .models import AccountCapitalState, AssetExposure, GateResult, LiquidityContext, TradeCapitalRequirement


def _floor_qty(value: Decimal, price: Decimal) -> Optional[int]:
    if price is None or price <= 0:
        return None
    if value <= 0:
        return 0
    return int((value / price).to_integral_value(rounding=ROUND_DOWN))


def check_gate1_basic_capital(
    requirement: TradeCapitalRequirement, state: AccountCapitalState, entry_price: Decimal
) -> GateResult:
    uncommitted = state.uncommitted_capital
    passed = requirement.required_capital <= uncommitted
    return GateResult(
        gate=FeasibilityGate.GATE_1_BASIC_CAPITAL_AVAILABILITY,
        passed=passed,
        constraint=BlockingConstraint.NONE if passed else BlockingConstraint.INSUFFICIENT_CASH,
        detail=(
            f"required_capital={requirement.required_capital} vs "
            f"uncommitted_capital={uncommitted} (cash={state.cash}, "
            f"committed_capital={state.committed_capital})"
        ),
        max_quantity_allowed=_floor_qty(uncommitted, entry_price),
    )


def check_gate2_capital_reserve(
    requirement: TradeCapitalRequirement, state: AccountCapitalState, entry_price: Decimal
) -> GateResult:
    deployable = state.deployable_capital
    passed = requirement.required_capital <= deployable
    return GateResult(
        gate=FeasibilityGate.GATE_2_CAPITAL_RESERVE,
        passed=passed,
        constraint=BlockingConstraint.NONE if passed else BlockingConstraint.MINIMUM_CASH_RESERVE,
        detail=(
            f"required_capital={requirement.required_capital} vs "
            f"deployable_capital={deployable} (reserved_capital={state.reserved_capital})"
        ),
        max_quantity_allowed=_floor_qty(deployable, entry_price),
    )


def check_gate3_position_allocation(
    position_value: Decimal,
    existing_asset_value: Decimal,
    total_capital: Decimal,
    entry_price: Decimal,
    config: CapitalFeasibilityConfig,
) -> GateResult:
    if total_capital <= 0:
        return GateResult(
            gate=FeasibilityGate.GATE_3_POSITION_ALLOCATION,
            passed=False,
            constraint=BlockingConstraint.INVALID_INPUT,
            detail="total_capital is non-positive; cannot evaluate allocation ratios.",
            max_quantity_allowed=0,
        )

    max_trade_value = (config.max_trade_capital_ratio * total_capital)
    max_asset_value = (config.max_asset_allocation_ratio * total_capital)

    trade_cap_ok = position_value <= max_trade_value
    post_trade_asset_value = existing_asset_value + position_value
    asset_cap_ok = post_trade_asset_value <= max_asset_value

    passed = trade_cap_ok and asset_cap_ok
    if not trade_cap_ok:
        constraint = BlockingConstraint.MAX_TRADE_CAPITAL_RATIO
    elif not asset_cap_ok:
        constraint = BlockingConstraint.MAX_ASSET_ALLOCATION
    else:
        constraint = BlockingConstraint.NONE

    # independent cap: room left under BOTH the trade-level and the
    # asset-level ceiling, whichever is tighter
    room_under_trade_cap = max_trade_value
    room_under_asset_cap = max_asset_value - existing_asset_value
    tightest_room = min(room_under_trade_cap, room_under_asset_cap)

    return GateResult(
        gate=FeasibilityGate.GATE_3_POSITION_ALLOCATION,
        passed=passed,
        constraint=constraint,
        detail=(
            f"position_value={position_value} (max_trade_value={max_trade_value}); "
            f"post_trade_asset_value={post_trade_asset_value} "
            f"(existing={existing_asset_value}, max_asset_value={max_asset_value})"
        ),
        max_quantity_allowed=_floor_qty(tightest_room, entry_price),
    )


def check_gate4_portfolio_exposure(
    position_value: Decimal,
    existing_sector_value: Optional[Decimal],
    existing_invested_capital: Decimal,
    total_capital: Decimal,
    entry_price: Decimal,
    config: CapitalFeasibilityConfig,
) -> GateResult:
    if total_capital <= 0:
        return GateResult(
            gate=FeasibilityGate.GATE_4_PORTFOLIO_EXPOSURE,
            passed=False,
            constraint=BlockingConstraint.INVALID_INPUT,
            detail="total_capital is non-positive; cannot evaluate exposure ratios.",
            max_quantity_allowed=0,
        )

    max_portfolio_value = config.max_portfolio_allocation_ratio * total_capital
    post_trade_portfolio_value = existing_invested_capital + position_value
    portfolio_cap_ok = post_trade_portfolio_value <= max_portfolio_value
    room_under_portfolio_cap = max_portfolio_value - existing_invested_capital

    sector_cap_ok = True
    room_under_sector_cap = None
    post_trade_sector_value = None
    max_sector_value = None
    if existing_sector_value is not None:
        max_sector_value = config.max_sector_allocation_ratio * total_capital
        post_trade_sector_value = existing_sector_value + position_value
        sector_cap_ok = post_trade_sector_value <= max_sector_value
        room_under_sector_cap = max_sector_value - existing_sector_value

    passed = portfolio_cap_ok and sector_cap_ok
    if not sector_cap_ok:
        constraint = BlockingConstraint.MAX_SECTOR_ALLOCATION
    elif not portfolio_cap_ok:
        constraint = BlockingConstraint.MAX_PORTFOLIO_ALLOCATION
    else:
        constraint = BlockingConstraint.NONE

    room_candidates = [room_under_portfolio_cap]
    if room_under_sector_cap is not None:
        room_candidates.append(room_under_sector_cap)
    tightest_room = min(room_candidates)

    return GateResult(
        gate=FeasibilityGate.GATE_4_PORTFOLIO_EXPOSURE,
        passed=passed,
        constraint=constraint,
        detail=(
            f"post_trade_portfolio_value={post_trade_portfolio_value} "
            f"(max_portfolio_value={max_portfolio_value}); "
            f"post_trade_sector_value={post_trade_sector_value} "
            f"(max_sector_value={max_sector_value})"
        ),
        max_quantity_allowed=_floor_qty(tightest_room, entry_price),
    )


def check_gate5_execution_capital(
    requirement: TradeCapitalRequirement, state: AccountCapitalState, entry_price: Decimal
) -> GateResult:
    """Re-checks affordability AT THE QUANTITY ACTUALLY BEING EVALUATED,
    using the real (non-linear) Cost Model output — a final sanity gate
    after gates 3/4 may have already reduced the candidate quantity
    (spec §13/§16 gate 5)."""
    deployable = state.deployable_capital
    passed = requirement.required_capital <= deployable
    return GateResult(
        gate=FeasibilityGate.GATE_5_EXECUTION_CAPITAL,
        passed=passed,
        constraint=BlockingConstraint.NONE if passed else BlockingConstraint.EXECUTION_COST_INFEASIBLE,
        detail=(
            f"At quantity={requirement.quantity}: required_capital="
            f"{requirement.required_capital} (position_value={requirement.position_value} + "
            f"execution_cost={requirement.execution_cost}) vs deployable_capital={deployable}"
        ),
        max_quantity_allowed=_floor_qty(deployable, entry_price),
    )


def check_gate6_liquidity(
    quantity: int,
    entry_price: Decimal,
    liquidity: LiquidityContext,
    config: CapitalFeasibilityConfig,
) -> GateResult:
    if liquidity.average_daily_traded_value is None:
        return GateResult(
            gate=FeasibilityGate.GATE_6_LIQUIDITY_EXECUTION,
            passed=True,
            constraint=BlockingConstraint.NONE,
            detail="No liquidity data supplied; gate is a no-op (spec §11 — absence never invents a number).",
            max_quantity_allowed=None,
        )

    max_participation_value = config.liquidity_participation_limit * liquidity.average_daily_traded_value
    position_value = Decimal(quantity) * entry_price
    passed = position_value <= max_participation_value

    return GateResult(
        gate=FeasibilityGate.GATE_6_LIQUIDITY_EXECUTION,
        passed=passed,
        constraint=BlockingConstraint.NONE if passed else BlockingConstraint.LIQUIDITY_PARTICIPATION_LIMIT,
        detail=(
            f"position_value={position_value} vs max_participation_value={max_participation_value} "
            f"({config.liquidity_participation_limit:.2%} of avg_daily_traded_value="
            f"{liquidity.average_daily_traded_value})"
        ),
        max_quantity_allowed=_floor_qty(max_participation_value, entry_price),
    )


def evaluate_all_gates(
    requirement: TradeCapitalRequirement,
    state: AccountCapitalState,
    exposure: AssetExposure,
    entry_price: Decimal,
    liquidity: LiquidityContext,
    config: CapitalFeasibilityConfig,
) -> List[GateResult]:
    """Runs gates 1-6, in order, at the candidate quantity described by
    `requirement`. Every gate is always evaluated (never short-circuited)
    so that ALL failing constraints are available for explainability
    (spec §16/§20), even though only the first failure determines the
    primary blocking gate."""
    g1 = check_gate1_basic_capital(requirement, state, entry_price)
    g2 = check_gate2_capital_reserve(requirement, state, entry_price)
    g3 = check_gate3_position_allocation(
        position_value=requirement.position_value,
        existing_asset_value=exposure.existing_value,
        total_capital=state.total_capital,
        entry_price=entry_price,
        config=config,
    )
    g4 = check_gate4_portfolio_exposure(
        position_value=requirement.position_value,
        existing_sector_value=exposure.existing_sector_value,
        existing_invested_capital=state.invested_capital,
        total_capital=state.total_capital,
        entry_price=entry_price,
        config=config,
    )
    g5 = check_gate5_execution_capital(requirement, state, entry_price)
    g6 = check_gate6_liquidity(requirement.quantity, entry_price, liquidity, config)
    return [g1, g2, g3, g4, g5, g6]
