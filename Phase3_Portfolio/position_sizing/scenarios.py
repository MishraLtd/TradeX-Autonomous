from decimal import Decimal
from typing import Optional

from .config import SizingConfig
from .enums import ScenarioType
from .models import PositionSizingRequest
from .risk_based import calculate_risk_based_size
from .volatility import calculate_volatility_adjustment
from .regime import calculate_regime_adjustment


def calculate_scenario_size(
    request: PositionSizingRequest,
    scenario: ScenarioType,
    config: SizingConfig,
) -> dict:
    """Derives conservative/stress sizes from explicit, documented assumption
    changes (extra slippage, vol-floor tightening, regime zeroing) — never an
    arbitrary percentage haircut (DESIGN.md Part 32)."""
    if request.stop_loss is None:
        return {"scenario": scenario.value, "quantity_raw": None, "note": "stop_loss missing"}

    if scenario == ScenarioType.BASE:
        cost = request.expected_total_cost
        vol_floor_override = None
    elif scenario == ScenarioType.CONSERVATIVE:
        cost = (request.expected_total_cost or Decimal("0")) * config.conservative_slippage_multiplier
        vol_floor_override = config.conservative_vol_mult_floor
    elif scenario == ScenarioType.STRESS:
        cost = (request.expected_total_cost or Decimal("0")) * config.stress_slippage_multiplier
        vol_floor_override = config.stress_vol_mult_floor
    else:
        raise ValueError(f"Unknown scenario {scenario}")

    base_size, diag = calculate_risk_based_size(
        request.entry_price,
        request.stop_loss,
        cost,
        request.capital_available_for_sizing,
        request.max_risk_allowed,
        config,
    )

    vol_mult = calculate_volatility_adjustment(request.atr_pct, config, vol_mult_floor_override=vol_floor_override)

    regime_mult = calculate_regime_adjustment(request.market_regime, config)
    if scenario == ScenarioType.STRESS and request.market_regime.value in config.stress_regime_zero_in:
        regime_mult = Decimal("0")

    scenario_qty_raw = base_size * vol_mult * regime_mult

    return {
        "scenario": scenario.value,
        "quantity_raw": scenario_qty_raw,
        "net_risk_per_share": diag["net_risk_per_share"],
        "volatility_mult": vol_mult,
        "regime_mult": regime_mult,
    }


def run_all_scenarios(request: PositionSizingRequest, config: SizingConfig) -> dict:
    return {
        s.value: calculate_scenario_size(request, s, config)
        for s in (ScenarioType.BASE, ScenarioType.CONSERVATIVE, ScenarioType.STRESS)
    }
