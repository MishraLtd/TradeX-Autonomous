"""
Centralized vocabulary for the Capital Feasibility Model, matching the
convention already established in position_sizing/enums.py and
portfolio_manager/enums.py: every status/constraint code is a documented
closed enum, never a free-form string invented at the call site.
"""
from enum import Enum


class FeasibilityStatus(str, Enum):
    """Final decision status (spec §18). Every value has one, and only
    one, documented meaning — never a vague "maybe"/"ok"."""

    FEASIBLE = "FEASIBLE"
    # Requested quantity is fully affordable and clears every gate at the
    # requested size. feasible_quantity == requested_quantity.

    PARTIALLY_FEASIBLE = "PARTIALLY_FEASIBLE"
    # Requested quantity is not fully affordable/permissible, but a
    # smaller positive quantity clears every gate. feasible_quantity is
    # that reduced size (0 < feasible_quantity < requested_quantity).

    CAPITAL_CONSTRAINED = "CAPITAL_CONSTRAINED"
    # Blocked specifically by Gate 1 (basic capital availability) or
    # Gate 2 (capital reserve) with feasible_quantity == 0 — there isn't
    # enough deployable capital for even one share once the reserve is
    # respected.

    PORTFOLIO_CONSTRAINED = "PORTFOLIO_CONSTRAINED"
    # Blocked specifically by Gate 3 (position allocation) or Gate 4
    # (portfolio/sector exposure) with feasible_quantity == 0 — cash
    # exists, but concentration limits leave no room at all.

    EXECUTION_CONSTRAINED = "EXECUTION_CONSTRAINED"
    # Blocked specifically by Gate 6 (liquidity/execution) with
    # feasible_quantity == 0 — capital and allocation would allow the
    # trade, but the position is too large relative to liquidity to be
    # realistically executed at any size the caller would accept.

    NOT_FEASIBLE = "NOT_FEASIBLE"
    # feasible_quantity == 0 for a reason that does not cleanly map to a
    # single one of the above (e.g. multiple simultaneous blocking gates,
    # or a structural/data problem outside gates 1-6). blocking_constraints
    # always lists every gate that failed.

    REJECTED_INVALID_INPUT = "REJECTED_INVALID_INPUT"
    # Input validation failed before any gate ran (bad price/quantity,
    # missing required upstream data, NaN, etc.). Never a silent 0.


class FeasibilityGate(str, Enum):
    """The seven-gate hierarchy (spec §16), evaluated in this order.
    The FIRST gate that fails is recorded as the primary blocking gate;
    ALL failing gates (not just the first) are preserved for
    explainability, per spec §16/§20."""

    GATE_1_BASIC_CAPITAL_AVAILABILITY = "GATE_1_BASIC_CAPITAL_AVAILABILITY"
    GATE_2_CAPITAL_RESERVE = "GATE_2_CAPITAL_RESERVE"
    GATE_3_POSITION_ALLOCATION = "GATE_3_POSITION_ALLOCATION"
    GATE_4_PORTFOLIO_EXPOSURE = "GATE_4_PORTFOLIO_EXPOSURE"
    GATE_5_EXECUTION_CAPITAL = "GATE_5_EXECUTION_CAPITAL"
    GATE_6_LIQUIDITY_EXECUTION = "GATE_6_LIQUIDITY_EXECUTION"
    GATE_7_FINAL_FEASIBILITY = "GATE_7_FINAL_FEASIBILITY"


class BlockingConstraint(str, Enum):
    """Machine-readable reason codes, one per gate plus a couple of
    input-validation codes. Mirrors the style of BindingConstraint in
    position_sizing/enums.py."""

    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    MINIMUM_CASH_RESERVE = "MINIMUM_CASH_RESERVE"
    MAX_TRADE_CAPITAL_RATIO = "MAX_TRADE_CAPITAL_RATIO"
    MAX_ASSET_ALLOCATION = "MAX_ASSET_ALLOCATION"
    MAX_SECTOR_ALLOCATION = "MAX_SECTOR_ALLOCATION"
    MAX_PORTFOLIO_ALLOCATION = "MAX_PORTFOLIO_ALLOCATION"
    EXECUTION_COST_INFEASIBLE = "EXECUTION_COST_INFEASIBLE"
    LIQUIDITY_PARTICIPATION_LIMIT = "LIQUIDITY_PARTICIPATION_LIMIT"
    LOT_SIZE_ROUNDING = "LOT_SIZE_ROUNDING"
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_REQUIRED_DATA = "MISSING_REQUIRED_DATA"
    NONE = "NONE"


class CapitalDataQuality(str, Enum):
    """Tri-state used wherever a financial figure can be legitimately
    unknown, distinguishing UNKNOWN from a real zero (spec §38), mirroring
    ValidationState in portfolio_manager/enums.py."""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    INVALID = "INVALID"
