"""
Regime taxonomy.

Design decision (Phase 2): we ship a SMALL production taxonomy of 7 states.
Transitional concepts (EARLY_TREND, TREND_EXHAUSTION, VOLATILITY_EXPANSION,
BREAKOUT_ENVIRONMENT, ...) are NOT separate class labels. Instead they are
modeled as *events on top of* the 7-state taxonomy via transitions.py
(regime_duration, transition_probability/direction, stress trajectory).

Rationale: every extra class multiplies the data required to estimate it
reliably (class-imbalance, PANIC especially), increases flapping risk, and
hurts interpretability. A transition is more honestly modeled as "derivative
of the state timeline" than as a first-class, independently-estimated label.
If out-of-sample evidence later shows a finer taxonomy earns its keep
(§9/§52), extend RegimeLabel — nothing else in the architecture assumes
exactly 7 members.
"""

from enum import Enum


class RegimeLabel(str, Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    SIDEWAYS = "SIDEWAYS"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    PANIC = "PANIC"
    UNKNOWN = "UNKNOWN"  # safe fallback state (§8) - not a "real" regime


class TrendState(str, Enum):
    STRONG_UP = "STRONG_UP"
    WEAK_UP = "WEAK_UP"
    FLAT = "FLAT"
    WEAK_DOWN = "WEAK_DOWN"
    STRONG_DOWN = "STRONG_DOWN"
    UNKNOWN = "UNKNOWN"


class VolatilityState(str, Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"
    UNKNOWN = "UNKNOWN"


class BreadthState(str, Enum):
    STRONG_POSITIVE = "STRONG_POSITIVE"
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    STRONG_NEGATIVE = "STRONG_NEGATIVE"
    UNKNOWN = "UNKNOWN"


class MomentumState(str, Enum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    UNKNOWN = "UNKNOWN"


class StressLevel(int, Enum):
    """§16 multi-factor market stress score. Ordinal, 0-3."""
    NORMAL = 0
    ELEVATED = 1
    SEVERE = 2
    PANIC = 3


class TradePermission(str, Enum):
    ALLOW = "ALLOW"
    REDUCE = "REDUCE"
    RESTRICT = "RESTRICT"
    BLOCK = "BLOCK"


class Timeframe(str, Enum):
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"
    W1 = "1w"


class TransitionDirection(str, Enum):
    TOWARD_TREND_UP = "TOWARD_TREND_UP"
    TOWARD_TREND_DOWN = "TOWARD_TREND_DOWN"
    TOWARD_SIDEWAYS = "TOWARD_SIDEWAYS"
    TOWARD_HIGH_VOL = "TOWARD_HIGH_VOL"
    TOWARD_LOW_VOL = "TOWARD_LOW_VOL"
    TOWARD_PANIC = "TOWARD_PANIC"
    TOWARD_RECOVERY = "TOWARD_RECOVERY"
    NONE = "NONE"
