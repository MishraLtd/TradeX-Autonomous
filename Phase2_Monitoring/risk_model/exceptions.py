class RiskModelError(Exception):
    """Base class for all risk-model errors."""


class InsufficientDataError(RiskModelError):
    """Raised when there isn't enough history to support a prediction."""


class LeakageError(RiskModelError):
    """Raised by validation utilities when a feature/label leakage
    invariant is violated (feature timestamp after entry timestamp, or
    overlapping train/test label windows without purge)."""


class DataQualityError(RiskModelError):
    """Raised when required input fields are missing, stale, or invalid."""
