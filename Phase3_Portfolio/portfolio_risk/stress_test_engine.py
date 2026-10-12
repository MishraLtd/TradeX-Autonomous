"""
stress_test_engine.py
------------------------
Correlated loss / stress risk (spec section 5.L).

Scenarios are fully configurable (see config.StressScenario) — nothing here
hardcodes an "unrealistic assumption" that can't be changed without editing
core logic. Each scenario estimates portfolio loss using:

  - market_shock: applied to every position, scaled by beta (defaults to 1.0
    if beta unavailable — flagged as a limitation)
  - sector_shock: applied only to positions in the (largest, or specified)
    sector
  - correlated_cluster_shock: applied to all positions in high-correlation
    clusters (from correlation_engine)
  - volatility_multiplier: scales the downside-deviation-based loss estimate
  - liquidity_discount: extra haircut applied to illiquid positions
    (from liquidity_risk_engine) to model exit slippage
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Set, Tuple

from .models import Position, DataQualityReport, DataQualityState
from .config import StressScenario
from .stop_loss_risk_engine import _correlated_clusters


@dataclass
class StressTestResult:
    scenario_losses_pct: Dict[str, float] = field(default_factory=dict)  # scenario name -> loss fraction of equity
    worst_scenario: str = ""
    worst_loss_pct: float = 0.0
    stress_score: float = 0.0


def run_stress_tests(
    positions: List[Position],
    portfolio_equity: float,
    sector_weights: Dict[str, float],
    high_corr_pairs: List[Tuple[str, str, float]],
    illiquid_symbols: List[str],
    downside_deviation_daily: float,
    scenarios: List[StressScenario],
    report: DataQualityReport,
) -> StressTestResult:
    open_positions = [p for p in positions if not p.is_proposed]
    if not open_positions or portfolio_equity <= 0:
        return StressTestResult()

    missing_beta = [p.symbol for p in open_positions if p.beta is None]
    if missing_beta:
        report.downgrade(
            DataQualityState.LIMITED_DATA,
            f"stress test: {missing_beta} have no beta — assumed beta=1.0 for market-shock scenarios",
        )

    largest_sector = max(sector_weights, key=sector_weights.get) if sector_weights else None
    clusters: List[Set[str]] = _correlated_clusters(high_corr_pairs)
    cluster_members: Set[str] = set()
    for c in clusters:
        cluster_members |= c

    results: Dict[str, float] = {}

    for scenario in scenarios:
        total_loss = 0.0

        for p in open_positions:
            weight = max(p.market_value, 0.0) / portfolio_equity
            beta = p.beta if p.beta is not None else 1.0
            position_loss_fraction = 0.0

            if scenario.market_shock != 0.0:
                position_loss_fraction += scenario.market_shock * beta

            if scenario.sector_shock != 0.0:
                target_sector = scenario.sector_target or largest_sector
                if p.sector == target_sector:
                    position_loss_fraction += scenario.sector_shock

            if scenario.correlated_cluster_shock != 0.0 and p.symbol in cluster_members:
                position_loss_fraction += scenario.correlated_cluster_shock

            if scenario.volatility_multiplier != 1.0 and downside_deviation_daily > 0:
                # scale the annualized-equivalent downside move by the multiplier
                position_loss_fraction += -downside_deviation_daily * (scenario.volatility_multiplier - 1.0) * 5

            if scenario.liquidity_discount != 0.0 and p.symbol in illiquid_symbols:
                position_loss_fraction += -abs(scenario.liquidity_discount)

            total_loss += weight * position_loss_fraction

        results[scenario.name] = total_loss

    if not results:
        return StressTestResult()

    worst_name = min(results, key=results.get)  # most negative
    worst_loss = results[worst_name]

    worst_loss_magnitude = abs(min(worst_loss, 0.0))
    # Score scales worst-case stress loss against a soft ceiling of 25% equity loss.
    score = min(100.0, (worst_loss_magnitude / 0.25) * 100)

    return StressTestResult(
        scenario_losses_pct=results,
        worst_scenario=worst_name,
        worst_loss_pct=worst_loss,
        stress_score=max(0.0, score),
    )
