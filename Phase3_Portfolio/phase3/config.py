"""
Phase 3 orchestration policy.

This config holds the four component configs (unmodified — each package
remains the owner of its own thresholds) plus the handful of decisions
that only exist BECAUSE the components are wired together:

  * how a portfolio risk_status becomes a sizing multiplier,
  * how many size <-> feasibility iterations to run,
  * what to do when a trade is affordable but risk-breaching,
  * whether partial fills are acceptable,
  * how the three different regime vocabularies map onto each other.

None of these belong inside any single component, which is exactly why
they live here rather than being smuggled into one package's config.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Tuple

from capital_feasibility.config import CapitalFeasibilityConfig
from portfolio_manager.config import PortfolioManagerConfig
from portfolio_risk.config import RiskConfig
from position_sizing.config import SizingConfig

# Portfolio Risk's actionable gate (portfolio_risk/recommendation_engine.py)
RISK_STATUS_ACCEPT = "ACCEPT"
RISK_STATUS_WARNING = "ACCEPT_WITH_WARNING"
RISK_STATUS_REDUCE = "REDUCE_EXPOSURE"
RISK_STATUS_REJECT = "REJECT_NEW_POSITION"
RISK_STATUS_EMERGENCY = "EMERGENCY_REDUCTION"


def _default_risk_multipliers() -> Dict[str, Decimal]:
    """risk_status -> Position Sizing `risk_multiplier`.

    Deliberately conservative and monotone: a worse portfolio risk state
    can only ever shrink a new position, never grow it (the multiplier is
    clamped to <= 1.0 inside position_sizing.engine anyway).
    """
    return {
        RISK_STATUS_ACCEPT: Decimal("1.00"),
        RISK_STATUS_WARNING: Decimal("0.75"),
        RISK_STATUS_REDUCE: Decimal("0.50"),
        RISK_STATUS_REJECT: Decimal("0.00"),
        RISK_STATUS_EMERGENCY: Decimal("0.00"),
    }


def _default_regime_to_sizing() -> Dict[str, str]:
    """portfolio_manager.MarketRegime -> position_sizing.MarketRegime.

    The two packages were specified independently and have different
    vocabularies. PM's TRENDING carries no direction, so it maps to
    TRENDING_UP only when the caller's regime confidence is high enough;
    otherwise the safest non-directional label is used. See regime.py.
    """
    return {
        "TRENDING": "TRENDING_UP",
        "RANGING": "SIDEWAYS",
        "VOLATILE": "HIGH_VOLATILITY",
        "UNKNOWN": "UNKNOWN",
    }


def _default_regime_to_risk() -> Dict[str, str]:
    """portfolio_manager.MarketRegime -> portfolio_risk regime label.

    portfolio_risk keys its limit multipliers off directional labels
    (BULLISH/BEARISH/...). PM's labels are not directional, so anything
    ambiguous maps to NEUTRAL or the explicitly conservative UNKNOWN
    rather than inventing a direction.
    """
    return {
        "TRENDING": "NEUTRAL",
        "RANGING": "NEUTRAL",
        "VOLATILE": "HIGH_VOLATILITY",
        "UNKNOWN": "UNKNOWN",
    }


@dataclass
class Phase3Config:
    version: str = "1.0.0"

    # --- component configs (each package still owns its own thresholds) ---
    portfolio_manager: PortfolioManagerConfig = field(default_factory=PortfolioManagerConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    feasibility: CapitalFeasibilityConfig = field(default_factory=CapitalFeasibilityConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # --- pre-trade risk -> sizing ---
    risk_status_multipliers: Dict[str, Decimal] = field(default_factory=_default_risk_multipliers)
    halt_risk_statuses: Tuple[str, ...] = (RISK_STATUS_REJECT, RISK_STATUS_EMERGENCY)

    apply_risk_position_cap: bool = True
    # Derive Position Sizing's `max_position_value` from the regime-adjusted
    # max_position_weight * portfolio equity (portfolio_risk/docs/INTEGRATION.md
    # section 4a). Turning this off lets the concentration limit be enforced
    # only after the fact, by the post-trade check.

    # --- sizing <-> feasibility loop ---
    max_sizing_iterations: int = 2
    # 1 = size once, then simply truncate to the affordable quantity.
    # 2 = feed the verified affordability figure back into Position Sizing
    #     once (closing the loop described in
    #     capital_feasibility/adapters/position_sizing_adapter.py) and
    #     re-verify. Higher values are allowed but rarely change anything,
    #     since feasibility is monotone in quantity.

    allow_partial_fill: bool = True
    # If the affordable quantity is smaller than the sized quantity, take
    # the smaller one (ALLOCATED_REDUCED) instead of dropping the trade.

    # --- post-trade risk ---
    risk_downsize_steps: Tuple[Decimal, ...] = (Decimal("0.5"), Decimal("0.25"))
    # When a trade is affordable but introduces a NEW limit breach, retry
    # at these fractions of the quantity before rejecting it. Each retry
    # is re-priced and re-gated — never assumed feasible because a larger
    # size was.

    reject_on_score_jump: bool = False
    max_acceptable_score_increase: float = 25.0
    # Optional soft policy: reject a trade that raises the portfolio risk
    # score by more than this even without breaching a hard limit. Off by
    # default — trade_impact_engine already flags it as CAUTION.

    # --- economics ---
    min_instruction_value: Decimal = Decimal("0")
    # Floor on position value for an emitted instruction. 0 = rely on
    # Position Sizing's own economic-viability test (min_expected_net_profit,
    # max_cost_to_edge_ratio), which is the intended owner of that question.

    # --- cost basis interpretation ---
    candidate_cost_basis: str = "PER_SHARE"
    # How to read PortfolioCandidate.expected_total_cost when
    # ExecutionInput.expected_cost_per_share is not supplied:
    #   "PER_SHARE"  — it is already a per-share round-trip estimate
    #                  (what position_sizing/DESIGN.md Part 4/12 expects)
    #   "PER_TRADE"  — it is a whole-trade figure at some reference size;
    #                  Phase 3 then refuses to guess the reference size and
    #                  leaves expected_total_cost unset, degrading Position
    #                  Sizing's economic check rather than fabricating a number.

    # --- regime vocabulary bridges ---
    regime_to_sizing: Dict[str, str] = field(default_factory=_default_regime_to_sizing)
    regime_to_risk: Dict[str, str] = field(default_factory=_default_regime_to_risk)
    directional_regime_confidence: float = 0.65
    # Below this regime_confidence, PM's TRENDING is treated as SIDEWAYS
    # for sizing rather than assuming an up-trend.

    def __post_init__(self) -> None:
        if self.candidate_cost_basis not in ("PER_SHARE", "PER_TRADE"):
            raise ValueError(
                f"candidate_cost_basis must be PER_SHARE or PER_TRADE, got {self.candidate_cost_basis!r}"
            )
        if self.max_sizing_iterations < 1:
            raise ValueError("max_sizing_iterations must be >= 1")


DEFAULT_CONFIG = Phase3Config()
