"""
PART 11 — MACHINE LEARNING INTERFACE

The ML model must NEVER rank opportunities on gross predicted return
alone. evaluate_trade_economics() is the only sanctioned bridge between
a model prediction and capital allocation: it always returns a
cost_adjusted_score, and PASS/REJECT is decided by the same gate as
every other caller (no ML-specific backdoor).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from .models import TradeInput, EconomicViability
from .engine import CostEngine
from .config import GateConfig, DEFAULT_GATE_CONFIG


@dataclass
class ModelPrediction:
    predicted_return_pct: Decimal          # e.g. 1.20 for +1.20%
    probability_of_profit: Decimal         # 0..1
    predicted_downside_pct: Optional[Decimal] = None   # e.g. -0.80 for -0.80%
    holding_period_days: Optional[int] = None


@dataclass
class TradeEconomicsResult:
    expected_gross_profit: Decimal
    expected_total_cost: Decimal
    expected_net_profit: Decimal
    expected_net_return_pct: Decimal
    expected_cost_ratio: Optional[Decimal]
    expected_break_even_move_pct: Decimal
    economic_viability: EconomicViability
    rejection_reasons: list
    cost_adjusted_score: Decimal


def evaluate_trade_economics(
    engine: CostEngine,
    trade: TradeInput,
    prediction: ModelPrediction,
    gate: GateConfig = DEFAULT_GATE_CONFIG,
) -> TradeEconomicsResult:
    """
    `trade` should have entry_price set and an exit_price implied by the
    prediction — the caller derives expected_exit_price from
    predicted_return_pct BEFORE calling this (kept explicit rather than
    hidden inside this function, so unit tests can pin exact prices).

    cost_adjusted_score = expected_net_return_pct * probability_of_profit
    — a simple, transparent, audit-friendly ranking score. It is
    intentionally NOT a Kelly-fraction or other more elaborate score:
    PART 24 says do not overengineer a ₹1,000-capital system, and a
    score the founder can hand-verify beats one that requires trusting
    a black box.
    """
    breakdown = engine.price_trade(trade, gate=gate)

    score = (breakdown.net_return_pct * prediction.probability_of_profit).quantize(Decimal("0.0001"))

    return TradeEconomicsResult(
        expected_gross_profit=breakdown.gross_pnl,
        expected_total_cost=breakdown.total_cost,
        expected_net_profit=breakdown.net_pnl,
        expected_net_return_pct=breakdown.net_return_pct,
        expected_cost_ratio=breakdown.cost_as_pct_of_gross_profit,
        expected_break_even_move_pct=breakdown.break_even_move_pct,
        economic_viability=breakdown.economic_viability,
        rejection_reasons=breakdown.rejection_reasons,
        cost_adjusted_score=score,
    )
