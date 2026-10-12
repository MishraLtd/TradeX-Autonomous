"""
Interfaces for future Phase 3 components (spec §33).

Phase 3A does NOT implement these — they exist only so the Portfolio
Manager's output (PortfolioDecision) has a clean, stable contract for the
next components to consume. No mock here fakes a production output; each
raises NotImplementedError until a real component is wired in.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict

from ..decision import PortfolioDecision


class PositionSizingProvider(ABC):
    """Given a PortfolioDecision's selected candidates, determines exact
    quantity per position. NOT implemented in Phase 3A."""

    @abstractmethod
    def size_positions(self, decision: PortfolioDecision) -> Dict[str, int]:
        raise NotImplementedError("Position Sizing is a future Phase 3 component.")


class CapitalFeasibilityProvider(ABC):
    """Validates affordability of sized positions against available capital.
    NOT implemented in Phase 3A."""

    @abstractmethod
    def check_feasibility(self, decision: PortfolioDecision, sized: Dict[str, int]) -> Dict[str, Any]:
        raise NotImplementedError("Capital Feasibility is a future Phase 3 component.")


class PortfolioRiskProvider(ABC):
    """Formal portfolio-level risk limits (VaR/CVaR/stop-loss budgets).
    NOT implemented in Phase 3A."""

    @abstractmethod
    def evaluate_risk(self, decision: PortfolioDecision) -> Dict[str, Any]:
        raise NotImplementedError("Portfolio Risk is a future Phase 3 component.")
