"""
Strongly-typed schemas (§33). Plain dataclasses (stdlib) rather than
pydantic, so the package has zero third-party dependencies - a deliberate
match to §19/§46 ("low computational cost", "no unnecessary infra").
Swap in pydantic later at the API boundary if TradeX needs JSON-schema
validation there; the internal contracts don't require it.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional
import json

from .enums import (
    RegimeLabel, TrendState, VolatilityState, BreadthState, MomentumState,
    StressLevel, TradePermission, Timeframe, TransitionDirection,
)


@dataclass(frozen=True)
class Bar:
    """One OHLCV observation. `timestamp` is the bar CLOSE time - the
    earliest instant at which this bar's values are legitimately knowable
    (§24 look-ahead prevention hinges on this convention being respected
    everywhere features.py touches a Bar)."""
    timestamp: float  # unix epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class InstrumentSeries:
    """A single instrument's (index / sector index / stock) bar history for
    one timeframe. Bars must be sorted ascending by timestamp; the caller
    (Feature Engine input adapter) is responsible for that invariant."""
    symbol: str
    timeframe: Timeframe
    bars: tuple  # tuple[Bar, ...] - immutable to prevent accidental mutation


@dataclass(frozen=True)
class BreadthSnapshot:
    """Cross-sectional breadth inputs for one timestamp (§3C)."""
    timestamp: float
    advancing: Optional[int] = None
    declining: Optional[int] = None
    pct_above_sma20: Optional[float] = None
    pct_above_sma50: Optional[float] = None
    pct_above_sma200: Optional[float] = None
    new_highs: Optional[int] = None
    new_lows: Optional[int] = None

    @property
    def is_available(self) -> bool:
        return self.pct_above_sma50 is not None or (
            self.advancing is not None and self.declining is not None
        )


@dataclass(frozen=True)
class DimensionScores:
    """Intermediate, per-dimension outputs (§3). Kept separate from the
    final RegimeState so every dimension is independently auditable and
    independently ablation-testable (§40)."""
    trend_state: TrendState = TrendState.UNKNOWN
    trend_score: float = 0.0          # signed, roughly [-1, 1]
    volatility_state: VolatilityState = VolatilityState.UNKNOWN
    volatility_percentile: Optional[float] = None
    breadth_state: BreadthState = BreadthState.UNKNOWN
    breadth_ratio: Optional[float] = None
    momentum_state: MomentumState = MomentumState.UNKNOWN
    momentum_score: float = 0.0
    stress_level: StressLevel = StressLevel.NORMAL
    stress_score: int = 0
    reason_codes: tuple = field(default_factory=tuple)


@dataclass(frozen=True)
class RegimeProbabilities:
    """§7 - not force-normalized unless the source model is a properly
    calibrated probabilistic model. The rule-based detector produces
    heuristic 'support scores', explicitly NOT probabilities; see
    rule_based.py docstring and calibration.py."""
    values: dict  # dict[str, float], keys are RegimeLabel.value
    is_calibrated_probability: bool = False

    def top(self):
        if not self.values:
            return None, 0.0
        label = max(self.values, key=self.values.get)
        return label, self.values[label]

    def entropy(self) -> Optional[float]:
        """Shannon entropy in bits, only meaningful if is_calibrated_probability."""
        import math
        vals = [v for v in self.values.values() if v > 0]
        if not vals:
            return None
        return -sum(v * math.log2(v) for v in vals)


@dataclass(frozen=True)
class StrategyCompatibility:
    values: dict  # dict[str, float] strategy_name -> compatibility score [0,1]

    def best(self):
        if not self.values:
            return None, 0.0
        name = max(self.values, key=self.values.get)
        return name, self.values[name]


@dataclass(frozen=True)
class RegimeState:
    """The final output object (§33, §55). Every field required for
    downstream consumers to make a decision without re-deriving anything
    from raw features themselves."""
    timestamp: float
    symbol_scope: str  # e.g. "MARKET:NIFTY50", "SECTOR:NIFTYBANK", "STOCK:RELIANCE"

    market_regime: RegimeLabel
    market_regime_confidence: float
    market_regime_probabilities: RegimeProbabilities

    short_term_regime: RegimeLabel
    medium_term_regime: RegimeLabel
    long_term_regime: RegimeLabel
    regime_alignment_score: float  # [0,1] agreement across timeframes

    regime_stability_score: float  # [0,1] - see transitions.py
    regime_duration_bars: int
    previous_regime: Optional[RegimeLabel]

    transition_probability: float
    transition_direction: TransitionDirection

    dimensions: DimensionScores

    sector_regime: Optional[RegimeLabel]
    stock_regime: Optional[RegimeLabel]
    cross_level_alignment_score: Optional[float]  # market/sector/stock agreement

    strategy_compatibility: StrategyCompatibility
    recommended_strategy: Optional[str]
    recommended_position_multiplier: float
    trade_permission: TradePermission

    data_freshness_ok: bool
    model_version: str
    feature_version: str
    config_version: str

    reason_codes: tuple

    def to_json(self) -> str:
        def default(o):
            if hasattr(o, "value"):
                return o.value
            if hasattr(o, "__dict__") or hasattr(o, "__dataclass_fields__"):
                return asdict(o)
            return str(o)
        return json.dumps(asdict(self), default=default, indent=2)
