"""
TradeX — Capital Feasibility Model (Phase 3, Component 3).

Public surface:

    from capital_feasibility import evaluate            # top-level convenience API (spec §31)
    from capital_feasibility.engine import evaluate_trade  # pure core, dependency-free
    from capital_feasibility.models import (
        TradeCapitalRequest, AccountCapitalState, AssetExposure,
        CapitalFeasibilityResult, GateResult, LiquidityContext,
    )
    from capital_feasibility.config import CapitalFeasibilityConfig, DEFAULT_CONFIG
    from capital_feasibility.enums import FeasibilityStatus, FeasibilityGate, BlockingConstraint

See DESIGN.md for the full specification this package implements.
"""
from .api import evaluate
from .config import CapitalFeasibilityConfig, DEFAULT_CONFIG
from .enums import BlockingConstraint, CapitalDataQuality, FeasibilityGate, FeasibilityStatus
from .models import (
    AccountCapitalState,
    AssetExposure,
    CapitalFeasibilityResult,
    GateResult,
    LiquidityContext,
    TradeCapitalRequest,
    TradeCapitalRequirement,
)

__version__ = "1.0.0"

__all__ = [
    "evaluate",
    "CapitalFeasibilityConfig",
    "DEFAULT_CONFIG",
    "BlockingConstraint",
    "CapitalDataQuality",
    "FeasibilityGate",
    "FeasibilityStatus",
    "AccountCapitalState",
    "AssetExposure",
    "CapitalFeasibilityResult",
    "GateResult",
    "LiquidityContext",
    "TradeCapitalRequest",
    "TradeCapitalRequirement",
]
