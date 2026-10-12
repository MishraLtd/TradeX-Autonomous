"""
PART — MAXIMUM FEASIBLE QUANTITY CALCULATION (spec §10, §13, §33)

max_quantity = MIN(capital_affordable_quantity, reserve_constrained_quantity,
                    portfolio_limit_quantity, asset_allocation_limit_quantity,
                    liquidity_limit_quantity, ...)

Costs are not strictly linear in quantity (fixed per-order components such
as the intraday brokerage cap and DP charges — see cost_model/calculators.py),
so this is not solved with a single formula. Instead: cheap, purely
arithmetic "naive" caps (ignoring transaction cost) from each gate give a
safe UPPER BOUND, then a binary search over that bounded range uses the
REAL Cost Model output at each candidate quantity to find the largest
quantity where every gate (including the cost-aware ones) passes.

Monotonicity assumption (documented, spec §33): required_capital(qty),
position_value(qty), and every allocation-ratio numerator used by the
gates are non-decreasing in qty. This holds for the current Cost Model
(brokerage/STT/slippage/impact are all non-decreasing functions of
turnover — see cost_model/calculators.py docstrings) — if "all gates
pass" at quantity Q, it therefore also holds at every quantity < Q, which
is exactly what binary search requires. `_is_monotonic_regression_guard`
does a defensive spot-check and falls back to a linear scan if it ever
detects the assumption doesn't hold for a given price function (never
silently trusts an assumption that turns out false for a given input).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from typing import Callable, List, Optional, Tuple

from .config import CapitalFeasibilityConfig
from .gates import evaluate_all_gates
from .models import AccountCapitalState, AssetExposure, GateResult, LiquidityContext, TradeCapitalRequirement

PriceFn = Callable[[int], TradeCapitalRequirement]


def _naive_upper_bound(
    state: AccountCapitalState,
    exposure: AssetExposure,
    entry_price: Decimal,
    liquidity: LiquidityContext,
    config: CapitalFeasibilityConfig,
) -> int:
    """Cheap, cost-ignoring caps used only to bound the search range —
    never used as the authoritative answer."""
    if entry_price is None or entry_price <= 0:
        return 0

    candidates = []

    candidates.append(max(Decimal("0"), state.deployable_capital) / entry_price)

    total_capital = state.total_capital
    if total_capital > 0:
        candidates.append((config.max_trade_capital_ratio * total_capital) / entry_price)
        candidates.append(
            max(Decimal("0"), config.max_asset_allocation_ratio * total_capital - exposure.existing_value)
            / entry_price
        )
        if exposure.existing_sector_value is not None:
            candidates.append(
                max(Decimal("0"), config.max_sector_allocation_ratio * total_capital - exposure.existing_sector_value)
                / entry_price
            )
        candidates.append(
            max(Decimal("0"), config.max_portfolio_allocation_ratio * total_capital - state.invested_capital)
            / entry_price
        )

    if liquidity.average_daily_traded_value is not None:
        candidates.append(
            (config.liquidity_participation_limit * liquidity.average_daily_traded_value) / entry_price
        )

    if not candidates:
        return 0

    naive_qty = min(candidates)
    if naive_qty <= 0:
        return 0
    # Small safety margin: costs only ever make the real feasible quantity
    # <= the naive one, but round generously up before flooring so the
    # search range never excludes the true answer due to Decimal rounding.
    return int((naive_qty * Decimal("1.05")).to_integral_value(rounding=ROUND_CEILING)) + 1


def _all_pass(gate_results: List[GateResult]) -> bool:
    return all(g.passed for g in gate_results)


def find_max_feasible_quantity(
    price_fn: PriceFn,
    state: AccountCapitalState,
    exposure: AssetExposure,
    entry_price: Decimal,
    liquidity: LiquidityContext,
    config: CapitalFeasibilityConfig,
    lot_size: int = 1,
) -> Tuple[int, TradeCapitalRequirement, List[GateResult]]:
    """Returns (max_feasible_quantity, requirement_at_that_quantity,
    gate_results_at_that_quantity)."""
    lot_size = max(1, lot_size)

    upper_bound = _naive_upper_bound(state, exposure, entry_price, liquidity, config)
    upper_lots = upper_bound // lot_size

    if upper_lots <= 0:
        req0 = price_fn(0)
        gates0 = evaluate_all_gates(req0, state, exposure, entry_price, liquidity, config)
        return 0, req0, gates0

    def gates_pass_at_lots(lots: int) -> Tuple[bool, TradeCapitalRequirement, List[GateResult]]:
        qty = lots * lot_size
        requirement = price_fn(qty)
        gate_results = evaluate_all_gates(requirement, state, exposure, entry_price, liquidity, config)
        return _all_pass(gate_results), requirement, gate_results

    # Defensive check: if even qty=0 fails a gate that only depends on
    # positive quantity that would itself be a bug, but 0 must always be
    # representable — evaluate it once up front.
    ok0, req0, gates0 = gates_pass_at_lots(0)

    lo, lo_req, lo_gates = 0, req0, gates0
    hi = upper_lots

    iterations = 0
    max_iterations = max(config.max_binary_search_iterations, upper_lots.bit_length() + 4)

    # Standard "largest true prefix" binary search over a monotonic
    # (non-increasing feasibility) predicate.
    while lo < hi and iterations < max_iterations:
        mid = (lo + hi + 1) // 2
        passed, mid_req, mid_gates = gates_pass_at_lots(mid)
        iterations += 1
        if passed:
            lo, lo_req, lo_gates = mid, mid_req, mid_gates
        else:
            hi = mid - 1

    # Defensive monotonicity guard: confirm nothing strictly above `lo`
    # (up to a small probe window) actually also passes, which would mean
    # the monotonicity assumption was violated for this input. If so, fall
    # back to a bounded linear scan upward from `lo` for correctness.
    probe = min(hi + 1, upper_lots)
    if probe > lo:
        probe_passed, probe_req, probe_gates = gates_pass_at_lots(probe)
        if probe_passed:
            lots = probe
            passed, req, gates = probe_passed, probe_req, probe_gates
            while lots + 1 <= upper_lots:
                nxt_passed, nxt_req, nxt_gates = gates_pass_at_lots(lots + 1)
                if not nxt_passed:
                    break
                lots, passed, req, gates = lots + 1, nxt_passed, nxt_req, nxt_gates
            return lots * lot_size, req, gates

    return lo * lot_size, lo_req, lo_gates
