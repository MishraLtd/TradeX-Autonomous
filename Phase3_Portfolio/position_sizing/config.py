"""
All tunable sizing parameters live here, as explicit bounded constants —
never fitted/optimized against a single backtest (Part 59, Part 26 self-audit).
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SizingConfig:
    version: str = "1.0.0"

    # --- risk budget ---
    default_risk_fraction: Decimal = Decimal("0.010")   # 1.0% of capital if no max_risk_allowed given
    hard_max_risk_fraction: Decimal = Decimal("0.020")   # absolute ceiling, 2.0% of capital, never exceeded

    # --- volatility adjustment bounds (Part 5) ---
    reference_atr_pct: Decimal = Decimal("0.015")        # 1.5% ATR treated as "normal"
    min_vol_mult: Decimal = Decimal("0.35")
    max_vol_mult: Decimal = Decimal("1.20")

    # --- confidence adjustment bounds (Part 6) ---
    min_confidence_mult: Decimal = Decimal("0.50")
    max_confidence_mult: Decimal = Decimal("1.10")
    confidence_ramp_low: Decimal = Decimal("0.50")
    confidence_ramp_high: Decimal = Decimal("0.80")

    # --- combined multiplier bounds (Part 9 / 17) — prevents compounding blowup ---
    min_combined_mult: Decimal = Decimal("0.15")
    max_combined_mult: Decimal = Decimal("1.15")

    # --- liquidity (Part 10) ---
    max_participation_pct: Decimal = Decimal("0.010")   # 1.0% of average traded value

    # --- regime multiplier map (Part 11) ---
    regime_multipliers: dict = None  # populated in __post_init__

    # --- Kelly (Part 8) ---
    kelly_fraction: Decimal = Decimal("0.15")            # 15% of full Kelly
    kelly_hard_cap: Decimal = Decimal("0.02")            # never more than 2% of capital, regardless

    # --- economic viability (Part 25 / 63) ---
    min_expected_net_profit: Decimal = Decimal("1.00")   # rupees; below this, position is not worth taking
    max_cost_to_edge_ratio: Decimal = Decimal("0.50")    # cost cannot eat more than 50% of expected gross edge

    # --- scenario haircuts (Part 32) — derived from documented stress assumptions, not arbitrary ---
    conservative_slippage_multiplier: Decimal = Decimal("1.5")   # 50% extra slippage assumption
    conservative_vol_mult_floor: Decimal = Decimal("0.50")
    stress_slippage_multiplier: Decimal = Decimal("2.5")          # 150% extra slippage assumption
    stress_vol_mult_floor: Decimal = Decimal("0.35")
    stress_regime_zero_in: tuple = ("HIGH_VOLATILITY", "PANIC")

    # --- rounding ---
    default_rounding_mode: str = "DOWN"

    def __post_init__(self):
        if self.regime_multipliers is None:
            object.__setattr__(
                self,
                "regime_multipliers",
                {
                    "TRENDING_UP": Decimal("1.00"),
                    "TRENDING_DOWN": Decimal("0.85"),
                    "SIDEWAYS": Decimal("0.90"),
                    "HIGH_VOLATILITY": Decimal("0.60"),
                    "LOW_VOLATILITY": Decimal("1.00"),
                    "PANIC": Decimal("0.00"),
                    "UNKNOWN": Decimal("0.50"),
                },
            )


DEFAULT_CONFIG = SizingConfig()
