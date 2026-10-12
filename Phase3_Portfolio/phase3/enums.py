"""
Phase 3 integration vocabulary.

Every component already has its own closed enum set (SizingStatus,
FeasibilityStatus, DecisionType, risk_status strings). This module adds
ONLY the vocabulary the integration layer itself needs — where a
candidate died in the chain, and what the run as a whole produced. It
never re-labels or collapses an upstream status: those are carried
through verbatim on the outcome record.
"""
from enum import Enum


class Phase3Status(str, Enum):
    OK = "OK"
    # Pipeline ran end to end. Zero or more instructions produced.

    NO_CANDIDATES = "NO_CANDIDATES"
    # Portfolio Manager selected nothing — nothing to size or fund.

    RISK_HALTED = "RISK_HALTED"
    # Portfolio Risk said REJECT_NEW_POSITION / EMERGENCY_REDUCTION on the
    # CURRENT portfolio before any new trade was considered. No candidate
    # is sized; existing-position actions from the PM are still returned.

    DECISION_UNAVAILABLE = "DECISION_UNAVAILABLE"
    # Portfolio Manager itself failed closed (bad portfolio/system state).

    PIPELINE_ERROR = "PIPELINE_ERROR"
    # A component raised (e.g. Cost Model could not price a leg). Fail
    # closed: no instructions are emitted from a partially-completed run.


class Stage(str, Enum):
    """Which component produced the terminal verdict for a candidate."""

    PORTFOLIO_MANAGER = "PORTFOLIO_MANAGER"
    PRE_TRADE_RISK = "PRE_TRADE_RISK"
    POSITION_SIZING = "POSITION_SIZING"
    CAPITAL_FEASIBILITY = "CAPITAL_FEASIBILITY"
    POST_TRADE_RISK = "POST_TRADE_RISK"
    EXECUTION_INPUT = "EXECUTION_INPUT"
    ALLOCATED = "ALLOCATED"


class AllocationStatus(str, Enum):
    ALLOCATED = "ALLOCATED"
    # Full requested size survived every stage.

    ALLOCATED_REDUCED = "ALLOCATED_REDUCED"
    # A positive size survived, smaller than Position Sizing's recommendation.
    # `reduced_by_stage` records which stage cut it.

    REJECTED_BY_PORTFOLIO_MANAGER = "REJECTED_BY_PORTFOLIO_MANAGER"
    REJECTED_MISSING_EXECUTION_INPUT = "REJECTED_MISSING_EXECUTION_INPUT"
    REJECTED_BY_SIZING = "REJECTED_BY_SIZING"
    REJECTED_BY_CAPITAL = "REJECTED_BY_CAPITAL"
    REJECTED_BY_PORTFOLIO_RISK = "REJECTED_BY_PORTFOLIO_RISK"
    SKIPPED_RISK_HALT = "SKIPPED_RISK_HALT"
