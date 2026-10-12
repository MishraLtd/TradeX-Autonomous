"""
TradeX Cost Model
==================

A production-grade trading-friction / cost model for a low-capital
(₹1,000 starting capital) algorithmic trading system built on Zerodha Kite.

This package is a DEFENSIVE component. Its job is to answer:

    "Does this predicted opportunity remain profitable after reality
     (statutory charges, broker charges, slippage, market impact) is
     applied?"

It is deliberately conservative: unknown costs are never assumed to be
zero, unknown fee schedules cause a hard failure (COST_MODEL_DATA_INCOMPLETE),
and every number produced is auditable back to a named rate with a source.
"""

from .engine import CostEngine
from .models import TradeInput, CostBreakdown, EconomicViability
from .exceptions import CostModelDataIncomplete, CostModelValidationError

__all__ = [
    "CostEngine",
    "TradeInput",
    "CostBreakdown",
    "EconomicViability",
    "CostModelDataIncomplete",
    "CostModelValidationError",
]
