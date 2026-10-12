"""
Central, versioned configuration for the Opportunity Scoring Model.

Design rule (spec §43): NOTHING is hard-coded inside the scoring logic.
Every threshold, weight, and reference range lives here, is named, has a
documented rationale, and is part of a versioned config object that gets
stamped onto every OpportunityAssessment for reproducibility (spec §41).

These defaults are Phase-1 heuristic starting points for a ~₹1,000
low-capital account. They are NOT the product of walk-forward calibration
yet — see README.md §8 "Weight Methodology" for the calibration plan
(Phase 2) and the transition criteria to Phase 3 (learned ranking).
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal
import hashlib
import json


@dataclass(frozen=True)
class ComponentWeights:
    """
    Weights for the Level-2 weighted-average synthesis (spec §21).
    Must sum to 1.0. Tail-risk (component I) and scenario resilience
    are deliberately NOT weighted members of this sum — they act as
    multiplicative dampeners instead. See README.md §6 for why.
    """

    net_edge: Decimal = Decimal("0.25")            # A
    risk_efficiency: Decimal = Decimal("0.20")     # C
    regime_compatibility: Decimal = Decimal("0.10")  # D
    prediction_reliability: Decimal = Decimal("0.10")  # E
    liquidity_execution: Decimal = Decimal("0.08")   # F
    capital_efficiency: Decimal = Decimal("0.12")    # G
    holding_time_efficiency: Decimal = Decimal("0.10")  # H
    cost_resilience: Decimal = Decimal("0.05")       # J

    def as_dict(self) -> dict:
        return {
            "net_edge": float(self.net_edge),
            "risk_efficiency": float(self.risk_efficiency),
            "regime_compatibility": float(self.regime_compatibility),
            "prediction_reliability": float(self.prediction_reliability),
            "liquidity_execution": float(self.liquidity_execution),
            "capital_efficiency": float(self.capital_efficiency),
            "holding_time_efficiency": float(self.holding_time_efficiency),
            "cost_resilience": float(self.cost_resilience),
        }

    def validate(self) -> None:
        total = sum(Decimal(str(v)) for v in self.as_dict().values())
        if abs(total - Decimal("1.0")) > Decimal("0.001"):
            raise ValueError(f"ComponentWeights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class NormalizationReferences:
    """
    Reference ranges used for Phase-1 "reference-range normalization"
    (min -> 0, target -> 100, capped). See README.md §7 "Normalization
    Methodology" for why we use fixed references instead of historical
    percentile normalization in Phase 1 (insufficient trade history to
    build stable percentile bands for a brand-new system), and the
    upgrade path to percentile/rank normalization in Phase 2.
    """

    net_return_floor_pct: Decimal = Decimal("0.0")     # 0% net return -> score 0
    net_return_target_pct: Decimal = Decimal("3.0")    # 3% net return -> score 100

    risk_efficiency_floor: Decimal = Decimal("0.0")    # net_return/risk ratio -> 0
    risk_efficiency_target: Decimal = Decimal("3.0")   # ratio of 3x -> 100

    capital_efficiency_floor_pct: Decimal = Decimal("0.0")
    capital_efficiency_target_pct: Decimal = Decimal("2.0")  # 2% of capital as net profit -> 100

    holding_efficiency_floor: Decimal = Decimal("0.0")     # net return %/day -> 0
    holding_efficiency_target: Decimal = Decimal("0.75")   # 0.75%/day -> 100

    cost_resilience_floor_ratio: Decimal = Decimal("1.0")   # cost = 100% of gross -> 0
    cost_resilience_target_ratio: Decimal = Decimal("0.10")  # cost = 10% of gross -> 100


@dataclass(frozen=True)
class HardGateThresholds:
    """
    Level-1 eligibility gates (spec §18). A candidate failing ANY gate
    is REJECTED before scoring; it never receives an opportunity_score.
    """

    min_net_return_pct: Decimal = Decimal("0.3")
    """Below this, the trade is not worth the operational/behavioral overhead
    even if nominally profitable, for a ₹1,000 account (see README §17)."""

    min_probability_of_profit: Decimal = Decimal("0.50")
    """Below coin-flip, the Return Model's directional signal is not
    considered actionable regardless of magnitude."""

    max_expected_downside_pct: Decimal = Decimal("5.0")
    """Absolute downside ceiling regardless of upside - catastrophic-loss
    guardrail independent of the Risk Engine's own limits."""

    max_probability_of_stop_loss: Decimal = Decimal("0.60")

    max_cost_ratio: Decimal = Decimal("0.60")
    """cost_ratio = expected_total_cost / expected_gross_return. Above this,
    the trade is economically fragile even if nominally net-positive."""

    max_break_even_move_vs_expected_move_ratio: Decimal = Decimal("0.85")
    """If the break-even move consumes more than this fraction of the
    expected favourable move, reject (spec §18)."""

    min_relative_liquidity_headroom: Decimal = Decimal("3.0")
    """average_traded_value must be >= this multiple of capital_required,
    else the position cannot be entered/exited without excessive impact."""

    max_relative_order_size: Decimal = Decimal("0.02")
    """position value must not exceed this fraction of average traded value."""

    max_spread_pct: Decimal = Decimal("1.0")

    min_model_calibration_quality: Decimal = Decimal("0.3")
    """0-1 scale supplied by upstream models; below this, the model's own
    self-reported calibration is too poor to trust at all (hard reject,
    distinct from the softer prediction_reliability *component* score)."""

    max_prediction_uncertainty: Decimal = Decimal("0.75")
    """0-1 scale; above this, treat as OOD / unreliable and reject."""

    max_data_age_seconds: int = 120
    """Any input snapshot older than this is considered stale (spec §45)."""

    max_holding_period_days: Decimal = Decimal("10")
    """Matches the delivery/swing horizon TradeX currently supports."""

    prohibited_regimes_by_default: tuple = ("PANIC",)
    """Regimes in which new trades are rejected unless a strategy explicitly
    opts in (spec §13, §18)."""


@dataclass(frozen=True)
class ScenarioConfig:
    """Scenario resilience configuration (spec §28)."""

    optimistic_weight: Decimal = Decimal("0.15")
    base_weight: Decimal = Decimal("0.55")
    conservative_weight: Decimal = Decimal("0.30")
    """Used to compute a blended 'scenario-aware' score alongside the
    three raw scenario scores. Conservative is weighted highest to bias
    the blended score toward robustness, per spec §28's stated preference
    for the trade with the flatter optimistic->conservative curve."""

    min_conservative_to_base_ratio: Decimal = Decimal("0.55")
    """If conservative_score / base_score falls below this, the resilience
    multiplier is capped low - a fragile opportunity should not rank near
    a robust one even with the same base score."""

    resilience_floor_multiplier: Decimal = Decimal("0.55")
    resilience_ceiling_multiplier: Decimal = Decimal("1.0")


@dataclass(frozen=True)
class ScoreBands:
    exceptional: Decimal = Decimal("90")
    strong: Decimal = Decimal("75")
    moderate: Decimal = Decimal("60")
    weak: Decimal = Decimal("40")
    # below `weak` => Poor

    def label_for(self, score: Decimal) -> str:
        if score >= self.exceptional:
            return "EXCEPTIONAL"
        if score >= self.strong:
            return "STRONG"
        if score >= self.moderate:
            return "MODERATE"
        if score >= self.weak:
            return "WEAK"
        return "POOR"


@dataclass(frozen=True)
class StabilityConfig:
    """spec §26 Score Stability."""

    minimum_material_score_change: Decimal = Decimal("3.0")
    """Rank/decision-facing consumers should treat score changes smaller
    than this as noise, not a genuine re-evaluation (hysteresis)."""

    perturbation_test_fraction: Decimal = Decimal("0.05")
    """Used by validation.sensitivity_analysis: perturb each numeric input
    by +/-5% and measure resulting score delta."""


@dataclass(frozen=True)
class OpportunityModelConfig:
    """Top-level, versioned configuration bundle."""

    config_version: str = "1.0.0-phase1"
    weights: ComponentWeights = field(default_factory=ComponentWeights)
    normalization: NormalizationReferences = field(default_factory=NormalizationReferences)
    gates: HardGateThresholds = field(default_factory=HardGateThresholds)
    scenarios: ScenarioConfig = field(default_factory=ScenarioConfig)
    bands: ScoreBands = field(default_factory=ScoreBands)
    stability: StabilityConfig = field(default_factory=StabilityConfig)

    top_k_max: int = 5
    """Max opportunities surfaced to the Portfolio Manager per cycle
    (spec §36) - a ceiling, not a target; fewer are returned if fewer pass."""

    tail_risk_penalty_strength: Decimal = Decimal("0.5")
    """0-1: how strongly component I (tail risk) can drag the score down
    multiplicatively. 0 = no effect, 1 = can zero out the score."""

    def __post_init__(self):
        self.weights.validate()

    def content_hash(self) -> str:
        """Stable hash of the full config, for input_hash/version tracking."""
        payload = json.dumps(_to_jsonable(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _to_jsonable(obj):
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, Decimal):
        return str(obj)
    return obj


DEFAULT_CONFIG = OpportunityModelConfig()
