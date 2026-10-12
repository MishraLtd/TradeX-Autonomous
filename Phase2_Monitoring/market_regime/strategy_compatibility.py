"""
§11/§12/§30 - strategy/regime compatibility.

The matrix below encodes the same directional intuition your spec lists in
§11 (momentum likes trends, mean-reversion likes sideways, etc.) as a
STARTING PRIOR only. §11 is explicit: "DO NOT simply hard-code these
mappings... a regime should be associated with strategies only if
backtesting demonstrates that relationship." So this module is built so the
matrix is a swappable, versioned config object (`STRATEGY_REGIME_PRIOR`)
that the `strategy_regime_performance` DB table (see db/schema.sql) is
designed to eventually replace with `expected_return`/`win_rate`-derived
scores per §12, once enough labeled trades exist per (strategy, regime)
cell (§27 minimum sample size).
"""

from __future__ import annotations
from typing import Dict, Optional

from .enums import RegimeLabel, VolatilityState
from .schemas import StrategyCompatibility

# score in [0,1], PRIOR ONLY - see module docstring
STRATEGY_REGIME_PRIOR: Dict[str, Dict[str, float]] = {
    "momentum":       {"TRENDING_UP": 0.85, "TRENDING_DOWN": 0.35, "SIDEWAYS": 0.25,
                        "HIGH_VOLATILITY": 0.40, "LOW_VOLATILITY": 0.45, "PANIC": 0.05},
    "mean_reversion": {"TRENDING_UP": 0.30, "TRENDING_DOWN": 0.30, "SIDEWAYS": 0.80,
                        "HIGH_VOLATILITY": 0.35, "LOW_VOLATILITY": 0.55, "PANIC": 0.05},
    "breakout":       {"TRENDING_UP": 0.70, "TRENDING_DOWN": 0.40, "SIDEWAYS": 0.45,
                        "HIGH_VOLATILITY": 0.55, "LOW_VOLATILITY": 0.65, "PANIC": 0.05},
    "defensive_flat": {"TRENDING_UP": 0.20, "TRENDING_DOWN": 0.55, "SIDEWAYS": 0.40,
                        "HIGH_VOLATILITY": 0.60, "LOW_VOLATILITY": 0.30, "PANIC": 0.70},
}

MIN_SAMPLE_SIZE_FOR_EMPIRICAL_OVERRIDE = 30  # §27, §12 - below this, fall back to prior


def compute_strategy_compatibility(
    regime: RegimeLabel,
    volatility_state: VolatilityState,
    regime_confidence: float,
    regime_stability: float,
    empirical_scores: Optional[Dict[str, Dict[str, float]]] = None,
    empirical_sample_sizes: Optional[Dict[str, Dict[str, int]]] = None,
) -> StrategyCompatibility:
    """Blends the prior matrix with empirical performance (if enough
    samples exist), then discounts everything by confidence/stability -
    an unstable, low-confidence regime read should mute ALL strategy
    scores, not just change which one wins (§30)."""
    values = {}
    regime_key = regime.value
    for strategy, row in STRATEGY_REGIME_PRIOR.items():
        prior = row.get(regime_key, 0.2)
        score = prior

        if empirical_scores and empirical_sample_sizes:
            n = empirical_sample_sizes.get(strategy, {}).get(regime_key, 0)
            if n >= MIN_SAMPLE_SIZE_FOR_EMPIRICAL_OVERRIDE:
                emp = empirical_scores.get(strategy, {}).get(regime_key)
                if emp is not None:
                    # shrink toward empirical estimate as sample size grows past minimum
                    weight = min(1.0, n / (n + MIN_SAMPLE_SIZE_FOR_EMPIRICAL_OVERRIDE))
                    score = (1 - weight) * prior + weight * emp

        # volatility suitability adjustment
        if volatility_state == VolatilityState.EXTREME:
            score *= 0.5
        elif volatility_state == VolatilityState.HIGH and strategy == "mean_reversion":
            score *= 0.7  # mean reversion is riskier when ranges are unstable

        discount = 0.5 + 0.5 * regime_confidence  # confidence in [0,1] -> discount [0.5,1.0]
        discount *= 0.5 + 0.5 * regime_stability

        values[strategy] = max(0.0, min(1.0, score * discount))

    return StrategyCompatibility(values=values)
