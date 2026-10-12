class PortfolioManagerError(Exception):
    """Base class for all Portfolio Manager errors."""


class InvalidPortfolioStateError(PortfolioManagerError):
    """Raised when PortfolioState is missing, malformed, or internally
    inconsistent (fail-closed, DESIGN PART 19)."""


class InvalidCandidateBatchError(PortfolioManagerError):
    """Raised when the entire candidate batch cannot be processed safely
    (e.g. not a list, corrupt structure) — distinct from individual
    candidates being INVALID, which is handled per-candidate instead."""
