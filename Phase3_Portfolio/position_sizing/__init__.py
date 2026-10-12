"""
TradeX Phase 3 / Component 2 — Position Sizing.

Given a Portfolio-Manager-approved opportunity, determine the position size.
This package does NOT decide whether to trade, whether the account can afford
the trade, or whether portfolio-level risk is acceptable — see DESIGN.md.
"""

from .engine import calculate_position_size
from .models import PositionSizingRequest, PositionSizeResult

__all__ = ["calculate_position_size", "PositionSizingRequest", "PositionSizeResult"]

__version__ = "0.1.0"
