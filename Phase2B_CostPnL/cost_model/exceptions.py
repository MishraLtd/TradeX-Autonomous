"""
Exceptions for the TradeX Cost Model.

Design rule (PART 18 — SAFETY AGAINST BAD DATA):
The Cost Model must never silently treat an unknown or missing cost as
zero. Every failure mode below is a FAIL CLOSED failure: callers (ML
Opportunity Engine, Portfolio Manager, Backtester, Live Execution Engine)
must catch these and treat the trade as NOT EVALUATED, never as FREE.
"""


class CostModelError(Exception):
    """Base class for all Cost Model errors."""


class CostModelDataIncomplete(CostModelError):
    """
    Raised when a rate, fee schedule, instrument fact, or liquidity
    input required to price a trade is missing, stale, or otherwise
    unusable.

    Carries a machine-readable `code` so callers can branch on the
    failure without parsing message text.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"COST_MODEL_DATA_INCOMPLETE[{code}]: {message}")


class CostModelValidationError(CostModelError):
    """Raised when a TradeInput is structurally invalid (negative price,
    zero quantity, impossible turnover, unknown enum value, etc.)."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"INVALID_INPUT[{field}]: {message}")


class StaleFeeScheduleError(CostModelDataIncomplete):
    """Raised when the fee schedule applicable at the trade's timestamp
    is past its known effective_to date and no newer schedule has been
    registered, i.e. the model would be guessing about current rates."""

    def __init__(self, segment: str, as_of, effective_to):
        super().__init__(
            code="STALE_FEE_SCHEDULE",
            message=(
                f"No fee schedule covers segment={segment!r} as_of={as_of}; "
                f"last known schedule expired {effective_to}. Refusing to "
                f"guess at current rates."
            ),
        )
