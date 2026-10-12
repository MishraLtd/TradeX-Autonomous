"""
TradeX Phase 3 — Component 1: Portfolio Manager.

Public entrypoint: `construct_portfolio`.
See DESIGN.md for the full design rationale.
"""

from .api import construct_portfolio
from .config import PortfolioManagerConfig
from .enums import (
    DecisionType,
    ExistingPositionAction,
    MarketRegime,
    RejectionReason,
    Strategy,
    TradeType,
    ValidationState,
)
from .candidate import PortfolioCandidate
from .portfolio_state import PortfolioPosition, PortfolioState, MarketContext
from .decision import PortfolioDecision

__version__ = "3.1.0-phase3a"

__all__ = [
    "construct_portfolio",
    "PortfolioManagerConfig",
    "DecisionType",
    "ExistingPositionAction",
    "MarketRegime",
    "RejectionReason",
    "Strategy",
    "TradeType",
    "ValidationState",
    "PortfolioCandidate",
    "PortfolioPosition",
    "PortfolioState",
    "MarketContext",
    "PortfolioDecision",
    "__version__",
]
