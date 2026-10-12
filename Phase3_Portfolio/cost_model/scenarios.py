"""
PART 14 — COST MODEL SCENARIO ANALYSIS

Runs OPTIMISTIC / BASE / CONSERVATIVE through the same engine (execution
-cost components only are scaled — see engine._SCENARIO_MULTIPLIERS) and
requires the trade to be net-profitable under a MAJORITY of scenarios,
not just the optimistic one, before being reported as "robustly viable".
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict

from .models import TradeInput, CostBreakdown, Scenario, EconomicViability
from .engine import CostEngine
from .config import GateConfig, DEFAULT_GATE_CONFIG


@dataclass
class ScenarioResult:
    breakdowns: Dict[Scenario, CostBreakdown]

    @property
    def robustly_viable(self) -> bool:
        """PASS requires BASE and CONSERVATIVE to both clear the gate —
        an optimistic-only pass is explicitly NOT sufficient (PART 14:
        'A trade should not automatically pass simply because the
        optimistic case is profitable.')."""
        base = self.breakdowns[Scenario.BASE]
        cons = self.breakdowns[Scenario.CONSERVATIVE]
        return (base.economic_viability == EconomicViability.PASS and
                cons.economic_viability == EconomicViability.PASS)

    def summary(self) -> Dict[str, Decimal]:
        return {s.value: self.breakdowns[s].net_pnl for s in Scenario}


def run_scenarios(engine: CostEngine, trade: TradeInput,
                   gate: GateConfig = DEFAULT_GATE_CONFIG,
                   calibrated_slippage_bps: Decimal = None) -> ScenarioResult:
    breakdowns = {
        scenario: engine.price_trade(
            trade, scenario=scenario, gate=gate,
            calibrated_slippage_bps=calibrated_slippage_bps,
        )
        for scenario in Scenario
    }
    return ScenarioResult(breakdowns=breakdowns)
