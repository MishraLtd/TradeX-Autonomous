"""
PART — FINAL FEASIBILITY DECISION + EXPLAINABILITY (spec §18-20)

Turns (requested_quantity, feasible_quantity, gate_results at both
quantities) into a FeasibilityStatus plus a human-readable explanation
and the machine-readable blocking_constraints list. Never returns a bare
boolean (spec §20).
"""
from __future__ import annotations

from typing import List, Tuple

from .enums import BlockingConstraint, FeasibilityStatus
from .models import GateResult


def _failing_constraints(gate_results: List[GateResult]) -> List[BlockingConstraint]:
    seen = []
    for g in gate_results:
        if not g.passed and g.constraint not in seen:
            seen.append(g.constraint)
    return seen


_CAPITAL_CONSTRAINTS = {
    BlockingConstraint.INSUFFICIENT_CASH,
    BlockingConstraint.MINIMUM_CASH_RESERVE,
    # Gate 5 ("Execution Capital") re-checks required_capital against
    # deployable_capital using the real, cost-inclusive number — the same
    # underlying affordability question as gates 1/2, just re-validated
    # at whatever quantity survived gates 3/4. It is a capital-affordability
    # signal, not a market-microstructure one, so it groups with the
    # capital constraints for status classification, not with Gate 6.
    BlockingConstraint.EXECUTION_COST_INFEASIBLE,
}
_PORTFOLIO_CONSTRAINTS = {
    BlockingConstraint.MAX_TRADE_CAPITAL_RATIO,
    BlockingConstraint.MAX_ASSET_ALLOCATION,
    BlockingConstraint.MAX_SECTOR_ALLOCATION,
    BlockingConstraint.MAX_PORTFOLIO_ALLOCATION,
}
_EXECUTION_CONSTRAINTS = {
    # Gate 6 only: a genuine "capital exists but the order is too large
    # relative to liquidity to be realistically filled" signal.
    BlockingConstraint.LIQUIDITY_PARTICIPATION_LIMIT,
}


def determine_status(
    requested_quantity: int,
    feasible_quantity: int,
    gate_results_at_requested: List[GateResult],
) -> Tuple[FeasibilityStatus, List[BlockingConstraint]]:
    """`gate_results_at_requested` are always evaluated AT THE ORIGINALLY
    REQUESTED quantity (never at the reduced feasible one) — the whole
    point of `blocking_constraints` is to explain WHY a request needed
    reducing/rejecting (spec §19-20's worked example reports exactly
    this), which is trivially empty if evaluated at a quantity that, by
    construction, already clears every gate."""
    blocking = _failing_constraints(gate_results_at_requested)

    if requested_quantity == 0:
        # Nothing was requested — trivially feasible, there is no
        # position to fund (spec §22 Case 3). Distinct from a NON-ZERO
        # request that gets reduced all the way down to 0, which is
        # NOT_FEASIBLE/CAPITAL_CONSTRAINED/etc. below.
        return FeasibilityStatus.FEASIBLE, []

    if feasible_quantity > 0 and feasible_quantity >= requested_quantity:
        return FeasibilityStatus.FEASIBLE, []

    if feasible_quantity > 0:
        # A positive but reduced quantity clears every gate; `blocking`
        # here describes what blocked the ORIGINAL requested size.
        return FeasibilityStatus.PARTIALLY_FEASIBLE, blocking

    # feasible_quantity == 0: classify by which family of gate blocked it.
    if not blocking:
        return FeasibilityStatus.NOT_FEASIBLE, blocking

    only = set(blocking)
    if only <= _CAPITAL_CONSTRAINTS:
        return FeasibilityStatus.CAPITAL_CONSTRAINED, blocking
    if only <= _PORTFOLIO_CONSTRAINTS:
        return FeasibilityStatus.PORTFOLIO_CONSTRAINED, blocking
    if only <= _EXECUTION_CONSTRAINTS:
        return FeasibilityStatus.EXECUTION_CONSTRAINED, blocking
    return FeasibilityStatus.NOT_FEASIBLE, blocking


def build_explanation(
    status: FeasibilityStatus,
    requested_quantity: int,
    feasible_quantity: int,
    maximum_feasible_quantity: int,
    blocking_at_requested: List[BlockingConstraint],
) -> str:
    if status == FeasibilityStatus.FEASIBLE:
        return (
            f"Requested quantity {requested_quantity} is fully affordable and clears every "
            f"capital/portfolio/liquidity gate."
        )
    if status == FeasibilityStatus.PARTIALLY_FEASIBLE:
        reasons = ", ".join(c.value for c in blocking_at_requested) or "capital/allocation limits"
        return (
            f"Requested quantity {requested_quantity} exceeds what is currently feasible "
            f"({reasons}); reduced to {feasible_quantity} shares, which clears every gate. "
            f"Maximum feasible quantity under current constraints is {maximum_feasible_quantity}."
        )
    reasons = ", ".join(c.value for c in blocking_at_requested) or "an unresolved constraint"
    return (
        f"No feasible quantity (0 shares) under current constraints: {reasons}. "
        f"Requested quantity was {requested_quantity}."
    )
