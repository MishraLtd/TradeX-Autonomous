"""
PART — MODEL ARCHITECTURE / PUBLIC ENGINE (spec §24, §31)

    Input Validation
          v
    Capital State Calculation      (capital_state.py, done by the caller
                                     via adapters before evaluate_trade())
          v
    Trade Capital Requirement      (adapters/cost_adapter.py, via price_fn)
          v
    Capital Constraint Evaluation  (gates.py, gates 1-2)
          v
    Portfolio Constraint Evaluation(gates.py, gates 3-4)
          v
    Liquidity/Execution Evaluation (gates.py, gates 5-6)
          v
    Maximum Feasible Quantity      (max_quantity.py)
          v
    Final Feasibility Decision     (explain.py)
          v
    Explanation / Diagnostics      (explain.py)

`evaluate_trade()` is the pure core: deterministic, no I/O, no mutation
of caller objects (spec §25). It takes a `price_fn` callback instead of
depending on cost_model directly, so this module has no hard import on
any other TradeX package — the wiring happens in `api.py`.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from . import gates as gate_checks
from .audit import new_audit_id, now_utc
from .config import CapitalFeasibilityConfig, DEFAULT_CONFIG
from .enums import BlockingConstraint, FeasibilityStatus
from .explain import build_explanation, determine_status
from .max_quantity import PriceFn, find_max_feasible_quantity
from .models import (
    AccountCapitalState,
    AssetExposure,
    CapitalFeasibilityResult,
    TradeCapitalRequest,
)


def _validate_request(request: TradeCapitalRequest, state: AccountCapitalState) -> Optional[str]:
    if request.entry_price is None or request.entry_price <= 0:
        return f"entry_price must be positive, got {request.entry_price!r}"
    if request.requested_quantity is None or request.requested_quantity < 0:
        return f"requested_quantity must be >= 0, got {request.requested_quantity!r}"
    if request.lot_size is None or request.lot_size <= 0:
        return f"lot_size must be a positive integer, got {request.lot_size!r}"
    if state is None:
        return "account capital state is missing"
    if state.total_capital is None:
        return "total_capital is missing (unknown != zero, spec §38 — cannot proceed)"
    if state.total_capital < 0:
        return f"total_capital cannot be negative, got {state.total_capital!r}"
    if state.cash is None:
        return "cash is missing (unknown != zero, spec §38 — cannot proceed)"
    # Negative cash IS a valid state to evaluate (an already over-drawn
    # account, spec §22 Case 2) — it is not a validation error, it will
    # simply fail every capital gate.
    return None


def _rejected_result(request: TradeCapitalRequest, reason: str) -> CapitalFeasibilityResult:
    return CapitalFeasibilityResult(
        position_id=request.position_id,
        opportunity_id=request.opportunity_id,
        symbol=request.symbol,
        capital_feasible=False,
        feasibility_status=FeasibilityStatus.REJECTED_INVALID_INPUT,
        requested_quantity=request.requested_quantity or 0,
        feasible_quantity=0,
        maximum_feasible_quantity=0,
        requested_position_value=Decimal("0"),
        feasible_position_value=Decimal("0"),
        required_capital=Decimal("0"),
        requested_required_capital=Decimal("0"),
        available_capital=Decimal("0"),
        remaining_capital=Decimal("0"),
        capital_utilization_before=None,
        capital_utilization_after=None,
        capital_reserve_required=Decimal("0"),
        capital_reserve_remaining=Decimal("0"),
        post_trade_asset_exposure_ratio=None,
        post_trade_sector_exposure_ratio=None,
        gate_results=[],
        blocking_constraints=[BlockingConstraint.INVALID_INPUT],
        warnings=[],
        explanation=f"Rejected before any gate ran: {reason}",
        max_affordable_quantity=0,
        max_affordable_value=Decimal("0"),
        model_versions=dict(request.model_versions),
        calculation_version=DEFAULT_CONFIG.version,
        calculation_timestamp=now_utc(),
        audit_id=new_audit_id(),
    )


def evaluate_trade(
    request: TradeCapitalRequest,
    account_state: AccountCapitalState,
    exposure: AssetExposure,
    price_fn: PriceFn,
    config: Optional[CapitalFeasibilityConfig] = None,
) -> CapitalFeasibilityResult:
    """The pure core. `price_fn(qty) -> TradeCapitalRequirement` is
    expected to price the BUY leg for exactly `qty` shares at
    `request.entry_price` (see adapters/cost_adapter.price_buy_leg) and
    to raise CostModelIntegrationError on failure — that exception is
    intentionally NOT caught here (spec §29: a costing failure must fail
    the trade closed, all the way up, never silently downgrade to
    NOT_FEASIBLE with a fabricated ₹0 cost)."""
    config = config or DEFAULT_CONFIG

    invalid_reason = _validate_request(request, account_state)
    if invalid_reason is not None:
        return _rejected_result(request, invalid_reason)

    requirement_at_requested = price_fn(request.requested_quantity)

    gates_at_requested = gate_checks.evaluate_all_gates(
        requirement_at_requested, account_state, exposure, request.entry_price, request.liquidity, config
    )

    max_feasible_qty, requirement_at_max, gates_at_max = find_max_feasible_quantity(
        price_fn=price_fn,
        state=account_state,
        exposure=exposure,
        entry_price=request.entry_price,
        liquidity=request.liquidity,
        config=config,
        lot_size=request.lot_size,
    )

    feasible_quantity = min(request.requested_quantity, max_feasible_qty)
    if feasible_quantity == max_feasible_qty:
        requirement_at_feasible = requirement_at_max
        gates_at_feasible = gates_at_max
    else:
        requirement_at_feasible = price_fn(feasible_quantity)
        gates_at_feasible = gate_checks.evaluate_all_gates(
            requirement_at_feasible, account_state, exposure, request.entry_price, request.liquidity, config
        )

    status, blocking_at_requested = determine_status(
        requested_quantity=request.requested_quantity,
        feasible_quantity=feasible_quantity,
        gate_results_at_requested=gates_at_requested,
    )

    warnings: List[str] = []
    if not account_state.committed_capital_is_known:
        warnings.append(
            "committed_capital (capital already earmarked for OTHER pending trades) was not "
            "supplied and defaulted to 0 — this is an assumption, not a verified zero (spec §38)."
        )
    if exposure.existing_sector_value is None:
        warnings.append("Sector exposure unknown — Gate 4 sector-concentration check was skipped.")
    if request.liquidity.average_daily_traded_value is None:
        warnings.append("No liquidity data supplied — Gate 6 (liquidity/execution) was a no-op.")
    if account_state.deployable_capital < 0:
        warnings.append(
            f"Account is already over-committed: deployable_capital={account_state.deployable_capital} "
            f"is negative before this trade is even considered."
        )

    post_trade_state_at_feasible_asset = exposure.existing_value + requirement_at_feasible.position_value
    post_trade_asset_ratio = (
        (post_trade_state_at_feasible_asset / account_state.total_capital).quantize(Decimal("0.000001"))
        if account_state.total_capital > 0
        else None
    )
    post_trade_sector_ratio = None
    if exposure.existing_sector_value is not None and account_state.total_capital > 0:
        post_trade_sector_value = exposure.existing_sector_value + requirement_at_feasible.position_value
        post_trade_sector_ratio = (post_trade_sector_value / account_state.total_capital).quantize(Decimal("0.000001"))

    capital_utilization_after = None
    if account_state.total_capital > 0:
        capital_utilization_after = (
            (account_state.invested_capital + requirement_at_feasible.required_capital) / account_state.total_capital
        ).quantize(Decimal("0.000001"))

    explanation = build_explanation(
        status=status,
        requested_quantity=request.requested_quantity,
        feasible_quantity=feasible_quantity,
        maximum_feasible_quantity=max_feasible_qty,
        blocking_at_requested=blocking_at_requested,
    )

    remaining_capital = account_state.deployable_capital - requirement_at_feasible.required_capital
    reserve_remaining = account_state.cash - requirement_at_feasible.required_capital - account_state.reserved_capital

    return CapitalFeasibilityResult(
        position_id=request.position_id,
        opportunity_id=request.opportunity_id,
        symbol=request.symbol,
        capital_feasible=status in (FeasibilityStatus.FEASIBLE, FeasibilityStatus.PARTIALLY_FEASIBLE),
        feasibility_status=status,
        requested_quantity=request.requested_quantity,
        feasible_quantity=feasible_quantity,
        maximum_feasible_quantity=max_feasible_qty,
        requested_position_value=requirement_at_requested.position_value,
        feasible_position_value=requirement_at_feasible.position_value,
        required_capital=requirement_at_feasible.required_capital,
        requested_required_capital=requirement_at_requested.required_capital,
        available_capital=account_state.deployable_capital,
        remaining_capital=remaining_capital,
        capital_utilization_before=account_state.capital_utilization,
        capital_utilization_after=capital_utilization_after,
        capital_reserve_required=account_state.reserved_capital,
        capital_reserve_remaining=reserve_remaining,
        post_trade_asset_exposure_ratio=post_trade_asset_ratio,
        post_trade_sector_exposure_ratio=post_trade_sector_ratio,
        gate_results=gates_at_feasible,
        blocking_constraints=blocking_at_requested,
        warnings=warnings,
        explanation=explanation,
        max_affordable_quantity=max_feasible_qty,
        max_affordable_value=(Decimal(max_feasible_qty) * request.entry_price).quantize(Decimal("0.01")),
        model_versions=dict(request.model_versions),
        calculation_version=config.version,
        calculation_timestamp=now_utc(),
        audit_id=new_audit_id(),
    )
