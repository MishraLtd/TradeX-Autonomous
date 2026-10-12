"""
All thresholds live here, in one place, versioned. Nothing in the rest of
the package hard-codes a number.

IMPORTANT / HONEST FLAG (§29, §46, "never manufacture precision"):
The numeric defaults below are *reasonable starting priors* drawn from
common technical-analysis conventions (e.g. ADX>25=trending, ATR percentile
buckets), NOT values calibrated against your historical NSE data. Section 29
of the spec explicitly says these must be determined empirically, not
assumed. Until you run the walk-forward calibration job (see
docs/ARCHITECTURE.md Phase 15), treat this as CONFIG_VERSION "v0-uncalibrated"
and do not size real capital off it without shadow-mode validation (§53).
"""

from dataclasses import dataclass, field


CONFIG_VERSION = "v0-uncalibrated"
MODEL_VERSION = "rule_based-0.1.0"
FEATURE_VERSION = "features-0.1.0"


@dataclass(frozen=True)
class TrendConfig:
    adx_strong_threshold: float = 25.0
    adx_flat_threshold: float = 15.0
    sma_fast: int = 20
    sma_slow: int = 50
    slope_lookback: int = 10
    # min |slope| (in % of price per bar, annualized-agnostic) to call it non-flat
    slope_flat_epsilon: float = 0.0005


@dataclass(frozen=True)
class VolatilityConfig:
    atr_period: int = 14
    percentile_lookback: int = 252  # ~1 trading year of daily bars
    low_pct: float = 0.20   # below 20th percentile -> LOW
    high_pct: float = 0.80  # above 80th percentile -> HIGH
    extreme_pct: float = 0.97


@dataclass(frozen=True)
class BreadthConfig:
    strong_positive_ratio: float = 0.70  # % of universe above SMA50
    positive_ratio: float = 0.55
    negative_ratio: float = 0.45
    strong_negative_ratio: float = 0.30


@dataclass(frozen=True)
class MomentumConfig:
    lookback_bars: int = 20
    positive_threshold: float = 0.0
    negative_threshold: float = 0.0


@dataclass(frozen=True)
class StressConfig:
    """§16 multi-factor stress score. Each factor contributes 0/1/2 sub-points;
    total is bucketed into StressLevel. This is a transparent additive score,
    not a black box, so the reason_codes stay meaningful (§34)."""
    index_drawdown_elevated: float = -0.03   # -3% from recent high
    index_drawdown_severe: float = -0.07
    intraday_return_elevated: float = -0.02  # single-session move
    intraday_return_severe: float = -0.04
    vol_percentile_elevated: float = 0.85
    vol_percentile_severe: float = 0.97
    breadth_collapse_ratio: float = 0.25     # <25% of universe above SMA50
    correlation_spike_z: float = 2.0         # z-score of avg pairwise corr
    volume_spike_z: float = 2.5
    # score thresholds (sum of contributing sub-scores, see stress.py)
    elevated_score: int = 2
    severe_score: int = 4
    panic_score: int = 6


@dataclass(frozen=True)
class ConfidenceConfig:
    """§29 - buckets are provisional priors pending empirical calibration."""
    low: float = 0.50
    cautious: float = 0.70
    normal: float = 0.85


@dataclass(frozen=True)
class StabilityConfig:
    """§18 hysteresis / anti-flapping."""
    min_regime_duration_bars: int = 3       # don't relabel before N bars
    confirmation_bars: int = 2              # need N consecutive raw votes to confirm a flip
    probability_smoothing_alpha: float = 0.35  # EWMA smoothing of raw scores


@dataclass(frozen=True)
class DataFreshnessConfig:
    max_staleness_seconds: dict = field(default_factory=lambda: {
        "5m": 90,
        "15m": 180,
        "1h": 600,
        "1d": 3600 * 20,
        "1w": 3600 * 24 * 3,
    })
    min_bars_required: dict = field(default_factory=lambda: {
        "5m": 60,
        "15m": 60,
        "1h": 60,
        "1d": 260,   # >1yr daily history for percentile features
        "1w": 52,
    })


@dataclass(frozen=True)
class RegimeConfig:
    trend: TrendConfig = field(default_factory=TrendConfig)
    volatility: VolatilityConfig = field(default_factory=VolatilityConfig)
    breadth: BreadthConfig = field(default_factory=BreadthConfig)
    momentum: MomentumConfig = field(default_factory=MomentumConfig)
    stress: StressConfig = field(default_factory=StressConfig)
    confidence: ConfidenceConfig = field(default_factory=ConfidenceConfig)
    stability: StabilityConfig = field(default_factory=StabilityConfig)
    freshness: DataFreshnessConfig = field(default_factory=DataFreshnessConfig)
    config_version: str = CONFIG_VERSION


DEFAULT_CONFIG = RegimeConfig()
