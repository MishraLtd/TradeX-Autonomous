"""
Rule-based baseline regime classifier (§5, §9).

Deliberately NOT a black box: label = whichever regime's heuristic "support
score" is highest, where each support score is a transparent weighted
combination of the dimension scores from trend.py/volatility.py/stress.py.

These support scores are explicitly *not* probabilities (§7) - we do not
force them to sum to 1, and we do not claim they're calibrated. A properly
calibrated probabilistic alternative (statistical detector / GMM / HMM /
GBM, per §9) is the natural Phase 6 upgrade; calibration.py has the hook
where its output would be wired in behind the exact same interface used
here, so nothing downstream needs to change when that day comes.
"""

from __future__ import annotations
from dataclasses import dataclass

from .enums import RegimeLabel, TrendState, VolatilityState, StressLevel
from .schemas import DimensionScores, RegimeProbabilities


def classify_regime(dims: DimensionScores) -> RegimeProbabilities:
    """Maps dimension scores -> heuristic support score per RegimeLabel.

    Panic takes precedence over everything else: a market can be
    "trending up" on moving averages while stress is at PANIC level (e.g.
    a violent V-shaped whipsaw), and §4/§16 are explicit that stress must
    not be silently overridden by a trend read.
    """
    scores = {label.value: 0.0 for label in RegimeLabel if label != RegimeLabel.UNKNOWN}

    # Unknown dimensions collectively -> UNKNOWN regime, handled by caller
    # (inference.py) via validator.py; classify_regime assumes dims are
    # already validated as sufficient.

    # --- Panic / stress axis (dominant) ---
    if dims.stress_level == StressLevel.PANIC:
        scores[RegimeLabel.PANIC.value] += 3.0
    elif dims.stress_level == StressLevel.SEVERE:
        scores[RegimeLabel.PANIC.value] += 1.2
        scores[RegimeLabel.HIGH_VOLATILITY.value] += 0.8

    # --- Volatility axis ---
    if dims.volatility_state == VolatilityState.EXTREME:
        scores[RegimeLabel.HIGH_VOLATILITY.value] += 1.5
        scores[RegimeLabel.PANIC.value] += 0.5
    elif dims.volatility_state == VolatilityState.HIGH:
        scores[RegimeLabel.HIGH_VOLATILITY.value] += 1.2
    elif dims.volatility_state == VolatilityState.LOW:
        scores[RegimeLabel.LOW_VOLATILITY.value] += 1.2

    # --- Trend axis ---
    if dims.trend_state in (TrendState.STRONG_UP, TrendState.WEAK_UP):
        weight = 1.3 if dims.trend_state == TrendState.STRONG_UP else 0.7
        scores[RegimeLabel.TRENDING_UP.value] += weight * (1.0 + max(0.0, dims.trend_score))
    elif dims.trend_state in (TrendState.STRONG_DOWN, TrendState.WEAK_DOWN):
        weight = 1.3 if dims.trend_state == TrendState.STRONG_DOWN else 0.7
        scores[RegimeLabel.TRENDING_DOWN.value] += weight * (1.0 + max(0.0, -dims.trend_score))
    elif dims.trend_state == TrendState.FLAT:
        scores[RegimeLabel.SIDEWAYS.value] += 1.2

    # --- Momentum corroborates trend (does not independently create a regime) ---
    scores[RegimeLabel.TRENDING_UP.value] += 0.3 * max(0.0, dims.momentum_score) * 10
    scores[RegimeLabel.TRENDING_DOWN.value] += 0.3 * max(0.0, -dims.momentum_score) * 10

    # --- Breadth corroborates trend/panic, only if available ---
    from .enums import BreadthState
    if dims.breadth_state == BreadthState.STRONG_POSITIVE:
        scores[RegimeLabel.TRENDING_UP.value] += 0.6
    elif dims.breadth_state == BreadthState.POSITIVE:
        scores[RegimeLabel.TRENDING_UP.value] += 0.3
    elif dims.breadth_state == BreadthState.STRONG_NEGATIVE:
        scores[RegimeLabel.TRENDING_DOWN.value] += 0.6
        scores[RegimeLabel.PANIC.value] += 0.3
    elif dims.breadth_state == BreadthState.NEGATIVE:
        scores[RegimeLabel.TRENDING_DOWN.value] += 0.3

    # cap floor at 0
    scores = {k: max(0.0, v) for k, v in scores.items()}

    # Hard override: an actual PANIC-level stress score (§16 threshold) must
    # win the argmax outright. A trend or momentum read computed over the
    # SAME crashing window will often score just as high on TRENDING_DOWN as
    # PANIC scores on its own axis - without this, "crashing hard" would
    # sometimes get classified as merely TRENDING_DOWN, which is exactly
    # the silent-downgrade-of-a-panic that §4/§16 forbid.
    if dims.stress_level == StressLevel.PANIC:
        scores[RegimeLabel.PANIC.value] = max(scores.values(), default=0.0) + 1.0

    return RegimeProbabilities(values=scores, is_calibrated_probability=False)


def heuristic_confidence(probs: RegimeProbabilities) -> float:
    """Confidence = normalized margin between top and runner-up support
    score, squashed into [0,1]. This is a heuristic separation measure,
    NOT a calibrated posterior probability (§28 calibration is a Phase-6+
    exercise requiring labeled historical data)."""
    values = sorted(probs.values.values(), reverse=True)
    if not values or values[0] == 0:
        return 0.0
    top = values[0]
    second = values[1] if len(values) > 1 else 0.0
    total = sum(values) or 1.0
    margin = (top - second) / total
    # squash: even a clean win over a crowded field shouldn't claim near-1.0
    # confidence from a heuristic score, so cap below the "strong" bucket
    # ceiling used in position_adjustment.py unless the margin is decisive.
    return max(0.0, min(0.97, margin * 1.6))
