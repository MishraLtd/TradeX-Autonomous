"""
TradeX Opportunity Scoring Model
=================================

A deterministic, auditable, capital-aware decision-layer that synthesizes
Return / Risk / Regime / Cost model outputs into a comparable Opportunity
Assessment for the Portfolio Manager.

See README.md in this repository for the full design documentation.
"""

from .schemas import (
    OpportunityCandidate,
    ReturnPrediction,
    RiskPrediction,
    RegimePrediction,
    CostPrediction,
    LiquidityInfo,
    PortfolioContext,
    OpportunityAssessment,
)
from .scoring import evaluate_opportunity
from .ranking import rank_opportunities
from .explainability import explain_opportunity
from .config import OpportunityModelConfig, DEFAULT_CONFIG

__version__ = "0.1.0-phase1"

__all__ = [
    "OpportunityCandidate",
    "ReturnPrediction",
    "RiskPrediction",
    "RegimePrediction",
    "CostPrediction",
    "LiquidityInfo",
    "PortfolioContext",
    "OpportunityAssessment",
    "evaluate_opportunity",
    "rank_opportunities",
    "explain_opportunity",
    "OpportunityModelConfig",
    "DEFAULT_CONFIG",
    "__version__",
]
