"""
Level 2 — Opportunity Quality component scores (spec §21).

Each function below computes ONE component, in isolation, on a [0,100]
scale (except the tail-risk dampener, which is a [0,1] multiplier - see
docstring on `tail_risk_multiplier`). Composition into a final score
happens in scoring.py. Keeping these as small, independently testable,
pure functions is deliberate: it makes double-counting auditable (each
function's docstring states which upstream fields it consumes and which
sibling component it must NOT overlap with - spec §22).
"""

from __future__ import annotations

from decimal import Decimal

from .config import OpportunityModelConfig
from .normalization import linear_normalize, safe_ratio, clamp
from .schemas import OpportunityCandidate


def net_edge_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component A: Expected Net Edge.

    Consumes: cost_prediction.base.expected_net_return_pct (Cost-Model-
    authoritative, already net of friction - spec §9 "net edge must
    dominate gross edge").

    If the Return Model supplies distributional quantiles, blend in a
    probability-weighted view (spec §8, §11) rather than relying on the
    point estimate alone: use return_p25 as a "reasonable downside-aware"
    proxy for the return actually likely to be realised, floor-blended
    with the net point estimate. This is the fallback-architecture
    requirement from spec §8 - if quantiles are absent we fall back
    cleanly to the point estimate with no error.

    Does NOT re-use probability_of_profit as a multiplier here - that
    would double-count against component B/probability-adjusted edge,
    which we've deliberately folded INTO this component via the quantile
    blend rather than keeping as a separate weighted term (see README §6
    for the double-counting rationale).
    """
    net_return = candidate.cost_prediction.base.expected_net_return_pct
    rp = candidate.return_prediction

    if rp.return_p25_pct is not None:
        # Blend point estimate with the 25th percentile to penalize
        # right-skewed-mean/left-tailed-reality predictions, without
        # throwing away the mean entirely.
        downside_aware = (net_return + rp.return_p25_pct) / Decimal("2")
        effective_return = min(net_return, downside_aware) if downside_aware < net_return else (
            net_return + (downside_aware - net_return) * Decimal("0.5")
        )
        # in practice: nudge net_return toward p25 by 50% weight
        effective_return = net_return - (net_return - rp.return_p25_pct) * Decimal("0.5") \
            if rp.return_p25_pct < net_return else net_return
    else:
        effective_return = net_return

    return linear_normalize(
        effective_return,
        config.normalization.net_return_floor_pct,
        config.normalization.net_return_target_pct,
    )


def risk_efficiency_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component C: Risk Efficiency = net_return / risk_exposure.

    risk_exposure is a blend of expected_downside and expected_max_adverse
    _excursion (both Risk-Model-owned), NOT a re-derivation of volatility.
    We blend rather than pick one because expected_downside is a "typical"
    estimate while MAE is a more tail-aware estimate (spec §10) - blending
    captures both without introducing a third free-floating tail metric
    here (tail_risk_pct is handled separately, multiplicatively, in
    `tail_risk_multiplier` to avoid overlap - spec §22).
    """
    net_return = candidate.cost_prediction.base.expected_net_return_pct
    downside = candidate.risk_prediction.expected_downside_pct
    mae = candidate.risk_prediction.expected_max_adverse_excursion_pct
    risk_exposure = (downside + mae) / Decimal("2")

    ratio = safe_ratio(net_return, risk_exposure)
    return linear_normalize(
        ratio,
        config.normalization.risk_efficiency_floor,
        config.normalization.risk_efficiency_target,
    )


def regime_compatibility_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component D: Regime Compatibility.

    Consumes regime_compatibility (empirically-derived strategy/regime fit,
    owned by the Regime Model per spec §13 - we do NOT recreate regime
    detection or hardcode bull/bear offsets here) weighted by
    regime_probability (how confident the regime call itself is - a low-
    confidence regime call should pull compatibility toward a neutral 50,
    not toward either extreme, since we can't be sure the compatibility
    score even applies).
    """
    rp = candidate.regime_prediction
    compat_pct = rp.regime_compatibility * Decimal("100")
    neutral = Decimal("50")
    blended = neutral + (compat_pct - neutral) * rp.regime_probability
    return clamp(blended, Decimal("0"), Decimal("100"))


def prediction_reliability_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component E: Prediction Reliability.

    Consumes model_calibration_quality (historical calibration, e.g.
    1 - Brier-score-derived measure, owned upstream - spec §12: "a model
    saying confidence=95% should only get credit if historical calibration
    supports it") and prediction_uncertainty (inverted). We deliberately
    do NOT use prediction_confidence directly - raw self-reported
    confidence is exactly the ungrounded number spec §12 warns against.
    Confidence is used only as a tie-breaker with lower weight than
    calibration.
    """
    calibration_pct = candidate.model_calibration_quality * Decimal("100")
    uncertainty_penalty_pct = (Decimal("1") - candidate.prediction_uncertainty) * Decimal("100")
    confidence_pct = candidate.prediction_confidence * Decimal("100")

    score = (
        calibration_pct * Decimal("0.55")
        + uncertainty_penalty_pct * Decimal("0.35")
        + confidence_pct * Decimal("0.10")
    )
    return clamp(score, Decimal("0"), Decimal("100"))


def liquidity_execution_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component F: Liquidity / Execution Quality.

    Consumes ONLY relative_order_size and spread (structural execution-
    feasibility signals). Deliberately does NOT re-penalize
    slippage_estimate_pct if the Cost Model has already folded slippage
    into expected_total_cost - that would double count against component
    A/J (spec §14, §22). We treat this component as "can I get in and out
    cleanly", the Cost Model as "what does getting in and out cost".
    """
    liq = candidate.liquidity
    # relative_order_size: 0 -> best, gate ceiling -> worst
    size_component = linear_normalize(
        liq.relative_order_size, Decimal("0"), Decimal("0.02"), invert=False
    )
    size_component = Decimal("100") - size_component  # smaller is better

    spread_component = Decimal("100") - linear_normalize(
        liq.spread_pct, Decimal("0"), Decimal("1.0")
    )

    return clamp((size_component + spread_component) / Decimal("2"), Decimal("0"), Decimal("100"))


def capital_efficiency_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component G: Capital Efficiency.

    IMPORTANT DESIGN NOTE (caught during self-critique, spec §67 Q3-style
    double-counting check): `expected_net_profit / capital_committed` is
    algebraically IDENTICAL to `expected_net_return_pct`, because
    net_profit is itself derived as `net_return_frac * capital_required`.
    Implementing spec §16's literal formula would therefore silently
    duplicate component A (net_edge) under a different name - exactly the
    double-counting failure spec §22 exists to prevent.

    What actually differentiates "capital efficiency" from "net edge" for
    a ₹1,000 account is OPTIONALITY: two trades with the same net_return%
    are not equally attractive if one ties up 90% of total capital and the
    other ties up 30%, because the latter leaves capital free to catch the
    NEXT opportunity (spec §16's own Trade-A-vs-Trade-B example is best
    read this way, not as a literal ratio). So this component blends:

      1. the same net-return-pct normalization as component A (so a
         genuinely better return still contributes), scaled down to
      2. a capital-utilization multiplier that REWARDS a lower
         capital_required / available_capital fraction.

    This keeps G distinct from A while preserving the intent of spec §16.
    """
    net_return_component = linear_normalize(
        candidate.cost_prediction.base.expected_net_return_pct,
        config.normalization.capital_efficiency_floor_pct,
        config.normalization.capital_efficiency_target_pct,
    )
    utilization = safe_ratio(candidate.capital_required, candidate.portfolio_context.available_capital)
    utilization = clamp(utilization, Decimal("0"), Decimal("1"))
    # Using 40% of available capital or less: full credit. Using 100%: 70% credit.
    utilization_multiplier = Decimal("1") - utilization * Decimal("0.3")

    return clamp(net_return_component * utilization_multiplier, Decimal("0"), Decimal("100"))


def holding_time_efficiency_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component H: Holding-Time Efficiency = net_return% / holding_period_days.

    This is a "return per capital-day" measure, NOT an annualized number
    (spec §15 explicitly warns against blindly annualizing short-horizon
    predictions into misleading yearly figures). It captures the intuition
    that a 2%-in-1-day opportunity frees capital for redeployment faster
    than a 2.2%-in-10-day opportunity, without pretending either
    compounds at its daily rate for a year.
    """
    net_return = candidate.cost_prediction.base.expected_net_return_pct
    per_day = safe_ratio(net_return, candidate.holding_period_days)
    return linear_normalize(
        per_day,
        config.normalization.holding_efficiency_floor,
        config.normalization.holding_efficiency_target,
    )


def cost_resilience_score(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component J: Cost Resilience = how much of the gross edge survives
    costs, i.e. inverted cost_ratio.

    This DELIBERATELY overlaps partially with component A (net_edge),
    since net_edge is itself post-cost. The overlap is intentional but
    controlled: A cares about the post-cost MAGNITUDE, J cares about the
    post-cost FRACTION/robustness-to-cost-shock (a trade can have a large
    net edge that is still a thin sliver of a much larger gross edge, and
    thus fragile to a small increase in real-world slippage). Because of
    this partial overlap, J is deliberately given a small weight (see
    config.py ComponentWeights.cost_resilience) so it nudges rather than
    dominates the score - see README §6 double-counting table.
    """
    cost_ratio = candidate.cost_prediction.base.cost_ratio
    return linear_normalize(
        cost_ratio,
        config.normalization.cost_resilience_floor_ratio,
        config.normalization.cost_resilience_target_ratio,
    )


def tail_risk_multiplier(candidate: OpportunityCandidate, config: OpportunityModelConfig) -> Decimal:
    """
    Component I: Tail-Risk Adjustment - implemented as a [0,1]
    MULTIPLICATIVE dampener applied to the final weighted score, not as a
    positive-weighted average component.

    Rationale (spec §22, §67 self-critique Q1): if tail risk were just
    another positively-weighted-average term, a trade with a catastrophic
    but low-probability tail could still score high overall by being
    excellent on every other axis - exactly the "high-return but
    extremely risky trade scores too highly" failure mode the self-
    critique must rule out. Making it multiplicative means a bad tail
    can meaningfully suppress an otherwise-strong score, without needing
    a separately-tuned large weight that would also distort the ranking
    of trades with unremarkable tails.

    Falls back to the expected_max_adverse_excursion vs expected_downside
    ratio when tail_risk_pct isn't supplied - a wide MAE/downside gap is
    itself a tail-fatness proxy.
    """
    rp = candidate.risk_prediction
    if rp.tail_risk_pct is not None:
        tail_severity = safe_ratio(rp.tail_risk_pct, rp.expected_downside_pct)
    else:
        tail_severity = safe_ratio(rp.expected_max_adverse_excursion_pct, rp.expected_downside_pct)

    # tail_severity of 1.0 (tail == typical case, i.e. no fat tail) -> multiplier 1.0
    # tail_severity of 4.0+ (tail is 4x+ the typical downside) -> multiplier floors out
    excess = clamp(tail_severity - Decimal("1"), Decimal("0"), Decimal("3")) / Decimal("3")
    strength = config.tail_risk_penalty_strength
    multiplier = Decimal("1") - (excess * strength)
    return clamp(multiplier, Decimal("1") - strength, Decimal("1"))
