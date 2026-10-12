class PositionSizingError(Exception):
    """Base exception. Note: the public engine API catches these and returns a
    typed SIZING_INCOMPLETE/NO_POSITION result instead of propagating — see
    DESIGN.md Part 25. These are raised internally / used in direct unit tests
    of the sub-functions."""


class InvalidRequestError(PositionSizingError):
    """Raised when required numeric inputs are structurally invalid
    (non-positive price, NaN, negative capital, etc.)."""


class MissingCriticalInputError(PositionSizingError):
    """Raised when a critical sizing input (e.g. stop_loss) is absent and no
    documented, explicitly-enabled fallback is configured."""


class HardConstraintViolation(PositionSizingError):
    """Raised internally if a computed quantity would violate a hard
    constraint after all caps were supposed to have been applied — indicates
    a bug in the engine, not a normal sizing outcome."""
