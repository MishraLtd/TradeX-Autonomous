"""
Exceptions for the Capital Feasibility Model.

Design rule (mirrors cost_model/exceptions.py): a missing or invalid
input must never be silently treated as zero/permissive. Structural
input problems raise here; `engine.evaluate()` catches these at the
boundary and returns a REJECTED_INVALID_INPUT result (never raises out
to the caller) so that batch evaluation (spec §15/§37) can continue
past one bad candidate.
"""


class CapitalFeasibilityError(Exception):
    """Base class for all Capital Feasibility Model errors."""


class InvalidTradeRequestError(CapitalFeasibilityError):
    """Raised when a TradeCapitalRequest is structurally invalid
    (negative/zero price, negative quantity, unknown enum value, etc.)."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"INVALID_INPUT[{field}]: {message}")


class MissingRequiredDataError(CapitalFeasibilityError):
    """Raised when a value required for a decision (not merely for an
    optional refinement) is missing — e.g. no account capital state at
    all. Carries a machine-readable `code` so callers can branch without
    parsing message text."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"CAPITAL_FEASIBILITY_DATA_INCOMPLETE[{code}]: {message}")


class CostModelIntegrationError(CapitalFeasibilityError):
    """Raised when the Cost Model cannot price the buy leg (e.g. it
    raised CostModelDataIncomplete/CostModelValidationError) and no
    documented fallback applies. Capital Feasibility never invents its
    own cost formula silently in place of a Cost Model failure (spec
    §29) — a costing failure fails the trade closed."""

    def __init__(self, message: str):
        super().__init__(f"COST_MODEL_INTEGRATION_ERROR: {message}")
