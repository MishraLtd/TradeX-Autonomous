"""
risk_budget_engine.py
------------------------
Portfolio risk budgets (spec section 13).

The total risk budget (100 units) is allocated across categories using the
same rationale as the composite score weights (config.DEFAULT_SCORE_WEIGHTS),
so the budget and the score stay internally consistent. Each category's
"consumption" is simply its current 0-100 sub-score expressed as a fraction
of its budget allocation.

This lets Position Sizing ask: "does the proposed trade consume more than
the remaining budget in any category?" via `remaining_budget`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from .config import RiskConfig


@dataclass
class RiskBudgetResult:
    total_budget: float = 100.0
    category_budget: Dict[str, float] = field(default_factory=dict)
    category_consumed: Dict[str, float] = field(default_factory=dict)  # in budget units
    category_remaining: Dict[str, float] = field(default_factory=dict)
    total_consumed: float = 0.0
    total_remaining: float = 0.0


def compute_risk_budget(
    config: RiskConfig,
    sub_scores: Dict[str, float],  # each 0-100
) -> RiskBudgetResult:
    total_budget = 100.0
    category_budget = {k: w * total_budget for k, w in config.score_weights.items()}

    category_consumed = {}
    category_remaining = {}
    for cat, budget in category_budget.items():
        sub_score = sub_scores.get(cat, 0.0)  # 0-100
        consumed = budget * (sub_score / 100.0)
        category_consumed[cat] = consumed
        category_remaining[cat] = max(0.0, budget - consumed)

    total_consumed = sum(category_consumed.values())
    total_remaining = max(0.0, total_budget - total_consumed)

    return RiskBudgetResult(
        total_budget=total_budget,
        category_budget=category_budget,
        category_consumed=category_consumed,
        category_remaining=category_remaining,
        total_consumed=total_consumed,
        total_remaining=total_remaining,
    )
