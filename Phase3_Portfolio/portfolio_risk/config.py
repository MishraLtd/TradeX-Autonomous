"""
config.py
---------
Central configuration for the TradeX Portfolio Risk Model.

Every threshold, weight, and policy knob referenced anywhere in the
portfolio_risk package lives here (or is passed in explicitly). Nothing
below is hardcoded inside a calculation engine.

All values are defaults and are meant to be overridden per-deployment,
per-capital-tier, or per-market-regime via `RiskConfig.override(...)`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List


# --------------------------------------------------------------------------
# Risk score component weights
# --------------------------------------------------------------------------
# These weights turn the individual risk-engine outputs (each normalized to
# 0-100) into a single portfolio_risk_score (0-100). They are NOT a blind
# average. Rationale for each weight:
#
#   concentration (0.15)     - single-name blowups are the most common way a
#                               small account is destroyed; weighted high.
#   sector (0.10)             - correlated-by-industry version of the above,
#                               slightly lower because it's usually a subset
#                               of the correlation signal too.
#   correlation (0.15)        - directly measures "false diversification",
#                               which is the core danger this model exists
#                               to catch (see project spec section 3).
#   volatility (0.10)         - portfolio sigma; important but already partly
#                               reflected in VaR/ES, so not double-weighted.
#   downside (0.10)           - asymmetric loss risk, distinct from symmetric
#                               volatility.
#   tail_risk (0.15)          - VaR/ES; the tail is what actually wipes out a
#                               small account, so it's weighted on par with
#                               concentration/correlation.
#   drawdown (0.10)           - current trajectory matters as much as
#                               instantaneous risk for a low-capital account
#                               that cannot statistically average out losses.
#   capital_utilization (0.05)- liquidity buffer; important but usually acts
#                               as a hard gate (see risk limits) rather than
#                               a graded score, so weighted lower here.
#   stop_loss (0.05)          - already a deterministic worst-case number;
#                               overlaps with downside/tail risk.
#   stress_test (0.05)        - scenario overlay; correlated with several of
#                               the above by construction, kept low to avoid
#                               double counting.
#
# Weights must sum to 1.0 (validated in scoring_engine).
DEFAULT_SCORE_WEIGHTS: Dict[str, float] = {
    "concentration": 0.15,
    "sector": 0.10,
    "correlation": 0.15,
    "volatility": 0.10,
    "downside": 0.10,
    "tail_risk": 0.15,
    "drawdown": 0.10,
    "capital_utilization": 0.05,
    "stop_loss": 0.05,
    "stress_test": 0.05,
}


# --------------------------------------------------------------------------
# Risk classification thresholds (applied to the final 0-100 score)
# --------------------------------------------------------------------------
DEFAULT_RISK_CLASS_THRESHOLDS = {
    "LOW": (0, 25),
    "MODERATE": (25, 45),
    "HIGH": (45, 65),
    "SEVERE": (65, 85),
    "CRITICAL": (85, 101),  # upper bound exclusive-ish; 100 included
}


# --------------------------------------------------------------------------
# Portfolio risk limits (hard/soft gates independent of the composite score)
# --------------------------------------------------------------------------
@dataclass
class RiskLimits:
    max_position_weight: float = 0.20          # single position / total equity
    warn_position_weight: float = 0.15

    max_sector_weight: float = 0.35
    warn_sector_weight: float = 0.25

    max_aggregate_stop_risk_pct: float = 0.08   # of total capital
    warn_aggregate_stop_risk_pct: float = 0.05

    max_portfolio_volatility: float = 0.35      # annualized
    warn_portfolio_volatility: float = 0.25

    max_var_95_pct: float = 0.06                # of capital, 1-day
    warn_var_95_pct: float = 0.04

    max_es_95_pct: float = 0.09
    warn_es_95_pct: float = 0.06

    max_positions: int = 12
    warn_positions: int = 9

    max_avg_correlation: float = 0.60
    warn_avg_correlation: float = 0.45
    max_pairwise_correlation: float = 0.85
    warn_pairwise_correlation: float = 0.70

    max_invested_capital_pct: float = 0.90
    warn_invested_capital_pct: float = 0.80
    min_cash_buffer_pct: float = 0.10           # required minimum idle cash

    max_drawdown_pct: float = 0.20
    warn_drawdown_pct: float = 0.12

    max_stress_loss_pct: float = 0.15           # worst configured scenario
    warn_stress_loss_pct: float = 0.10

    max_liquidity_ratio: float = 0.10           # position size / typical traded value
    warn_liquidity_ratio: float = 0.05


# --------------------------------------------------------------------------
# Stress test scenarios
# --------------------------------------------------------------------------
@dataclass
class StressScenario:
    name: str
    description: str
    market_shock: float = 0.0        # applied to all positions via beta
    sector_shock: float = 0.0        # applied only to the largest sector
    sector_target: str | None = None  # None => infer largest sector at runtime
    correlated_cluster_shock: float = 0.0  # applied to highly-correlated cluster
    volatility_multiplier: float = 1.0     # scales downside-deviation based loss
    liquidity_discount: float = 0.0        # extra haircut modeling exit slippage


DEFAULT_STRESS_SCENARIOS: List[StressScenario] = [
    StressScenario("market_down_2pct", "Broad market falls 2%", market_shock=-0.02),
    StressScenario("market_down_5pct", "Broad market falls 5%", market_shock=-0.05),
    StressScenario("sector_down_5pct", "Largest sector falls 5%", sector_shock=-0.05),
    StressScenario("sector_down_10pct", "Largest sector falls 10%", sector_shock=-0.10),
    StressScenario(
        "correlated_cluster_down_7pct",
        "Highly correlated cluster (pairwise corr > threshold) falls 7% together",
        correlated_cluster_shock=-0.07,
    ),
    StressScenario(
        "volatility_shock",
        "Volatility doubles; downside deviation loss scaled 2x",
        volatility_multiplier=2.0,
    ),
    StressScenario(
        "liquidity_deterioration",
        "Illiquid positions incur an extra 3% exit slippage",
        liquidity_discount=0.03,
    ),
]


# --------------------------------------------------------------------------
# Capital tiers — low-capital design (spec section 14)
# --------------------------------------------------------------------------
# Used only to select a human-readable "capital regime" label and to decide
# whether diversification-related warnings should be suppressed as
# economically infeasible. Does NOT change the underlying math.
CAPITAL_TIERS = [
    (0, 3000, "MICRO"),
    (3000, 10000, "VERY_SMALL"),
    (10000, 50000, "SMALL"),
    (50000, 200000, "MODERATE"),
    (200000, float("inf"), "STANDARD"),
]

# Below this many positions, concentration/sector warnings are annotated as
# "may be economically unavoidable at this capital level" rather than
# suppressed — the model must never hide risk, only contextualize it.
MIN_POSITIONS_FOR_FULL_DIVERSIFICATION = 5


# --------------------------------------------------------------------------
# General numerical / statistical knobs
# --------------------------------------------------------------------------
@dataclass
class RiskConfig:
    score_weights: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SCORE_WEIGHTS))
    risk_class_thresholds: Dict[str, tuple] = field(
        default_factory=lambda: dict(DEFAULT_RISK_CLASS_THRESHOLDS)
    )
    limits: RiskLimits = field(default_factory=RiskLimits)
    stress_scenarios: List[StressScenario] = field(
        default_factory=lambda: list(DEFAULT_STRESS_SCENARIOS)
    )

    # Correlation / covariance
    min_history_days: int = 30          # below this -> LIMITED_DATA / INSUFFICIENT_HISTORY
    preferred_history_days: int = 90
    correlation_high_threshold: float = 0.70   # used to build "high correlation pairs"
    covariance_shrinkage: float = 0.10         # Ledoit-Wolf-style shrinkage toward diagonal

    # VaR / ES
    var_confidence_levels: List[float] = field(default_factory=lambda: [0.90, 0.95, 0.99])
    default_var_confidence: float = 0.95
    var_horizon_days: int = 1

    # Drawdown
    drawdown_states: Dict[str, tuple] = field(
        default_factory=lambda: {
            "NORMAL": (0.0, 0.05),
            "ELEVATED": (0.05, 0.10),
            "HIGH": (0.10, 0.20),
            "CRITICAL": (0.20, 1.01),
        }
    )

    # Liquidity
    liquidity_lookback_days: int = 20

    # Regime-aware risk-tolerance multipliers applied to `limits` thresholds.
    # >1.0 loosens a limit, <1.0 tightens it. Applied multiplicatively.
    regime_multipliers: Dict[str, float] = field(
        default_factory=lambda: {
            "STRONG_BULLISH": 1.15,
            "BULLISH": 1.05,
            "NEUTRAL": 1.00,
            "BEARISH": 0.85,
            "STRONG_BEARISH": 0.65,
            "HIGH_VOLATILITY": 0.75,
            "UNKNOWN": 0.90,  # be conservative when regime is unknown
        }
    )

    def override(self, **kwargs) -> "RiskConfig":
        """Return a new RiskConfig with the given top-level fields replaced."""
        return replace(self, **kwargs)

    def limits_for_regime(self, regime: str) -> RiskLimits:
        """
        Return a new RiskLimits object with thresholds scaled by the
        regime multiplier. Multiplier only loosens/tightens; it never
        changes which direction a limit points.
        """
        mult = self.regime_multipliers.get(regime, self.regime_multipliers.get("UNKNOWN", 0.9))
        base = self.limits
        scaled = RiskLimits(
            max_position_weight=base.max_position_weight * mult,
            warn_position_weight=base.warn_position_weight * mult,
            max_sector_weight=base.max_sector_weight * mult,
            warn_sector_weight=base.warn_sector_weight * mult,
            max_aggregate_stop_risk_pct=base.max_aggregate_stop_risk_pct * mult,
            warn_aggregate_stop_risk_pct=base.warn_aggregate_stop_risk_pct * mult,
            max_portfolio_volatility=base.max_portfolio_volatility * mult,
            warn_portfolio_volatility=base.warn_portfolio_volatility * mult,
            max_var_95_pct=base.max_var_95_pct * mult,
            warn_var_95_pct=base.warn_var_95_pct * mult,
            max_es_95_pct=base.max_es_95_pct * mult,
            warn_es_95_pct=base.warn_es_95_pct * mult,
            max_positions=base.max_positions,  # count limits not regime-scaled
            warn_positions=base.warn_positions,
            max_avg_correlation=base.max_avg_correlation,  # correlation is structural, not regime-based
            warn_avg_correlation=base.warn_avg_correlation,
            max_pairwise_correlation=base.max_pairwise_correlation,
            warn_pairwise_correlation=base.warn_pairwise_correlation,
            max_invested_capital_pct=base.max_invested_capital_pct * mult,
            warn_invested_capital_pct=base.warn_invested_capital_pct * mult,
            min_cash_buffer_pct=base.min_cash_buffer_pct / mult if mult > 0 else base.min_cash_buffer_pct,
            max_drawdown_pct=base.max_drawdown_pct * mult,
            warn_drawdown_pct=base.warn_drawdown_pct * mult,
            max_stress_loss_pct=base.max_stress_loss_pct * mult,
            warn_stress_loss_pct=base.warn_stress_loss_pct * mult,
            max_liquidity_ratio=base.max_liquidity_ratio,  # liquidity is structural
            warn_liquidity_ratio=base.warn_liquidity_ratio,
        )
        return scaled


def capital_tier(total_capital: float) -> str:
    for lo, hi, label in CAPITAL_TIERS:
        if lo <= total_capital < hi:
            return label
    return "STANDARD"


DEFAULT_CONFIG = RiskConfig()
