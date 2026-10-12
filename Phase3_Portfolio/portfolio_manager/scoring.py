"""
Portfolio Contribution Score engine (spec §18, DESIGN PART 6).

score_candidate() computes a fully decomposed, normalized, additive score
for one candidate relative to a *given* set of already-selected symbols
(existing positions + candidates picked so far in the greedy loop). This
function is re-invoked for every candidate at every step of construction
so that "diversification benefit" and "correlation penalty" always reflect
the portfolio-as-it-would-be, not a static pre-computed value (DESIGN
PART 11, spec §12 example A/B/C).
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from .candidate import PortfolioCandidate
from .config import PortfolioManagerConfig
from .correlation import CorrelationProvider
from .enums import RejectionReason, SoftPreference, ValidationState
from .exposure import ExposureTracker
from .normalization import min_max_normalize
from .portfolio_state import MarketContext


@dataclass(frozen=True)
class CandidateScore:
    total: float
    components: Dict[str, float]
    soft_flags: List[SoftPreference]
    insufficient_data: bool


def score_candidate(
    candidate: PortfolioCandidate,
    *,
    portfolio_symbols: List[str],
    portfolio_sectors: List[str],
    portfolio_strategies: List[str],
    correlation_provider: CorrelationProvider,
    context: MarketContext,
    config: PortfolioManagerConfig,
) -> CandidateScore:
    w = config.weights
    components: Dict[str, float] = {}
    soft_flags: List[SoftPreference] = []
    insufficient_data = False

    # --- Return quality ---
    net_return_quality = min_max_normalize(
        candidate.expected_net_return, config.expected_net_return_range
    )
    components["return_contribution"] = w.return_weight * net_return_quality

    # --- Risk quality (lower risk -> higher score) ---
    risk_quality = min_max_normalize(candidate.expected_risk, config.expected_risk_range)
    components["risk_contribution"] = w.risk_weight * (1.0 - risk_quality)

    # --- Confidence ---
    confidence_norm = min_max_normalize(candidate.confidence, config.confidence_range)
    components["confidence_contribution"] = w.confidence_weight * confidence_norm
    if candidate.confidence < config.min_confidence:
        soft_flags.append(SoftPreference.LOW_CONFIDENCE)

    # --- Economic / cost quality ---
    # cost_ratio compares expected_total_cost against expected_net_profit —
    # both rupee-denominated (unlike expected_net_return, which is a
    # fraction) — so the ratio is dimensionally meaningful. A non-positive
    # net profit means costs are eroding an already-losing trade, so cost
    # quality is treated as worst-case rather than a misleadingly generous
    # near-zero ratio.
    if candidate.expected_net_profit > 0:
        cost_ratio = candidate.expected_total_cost / max(candidate.expected_net_profit, 1e-9)
    else:
        cost_ratio = config.cost_ratio_range[1]
    cost_quality = 1.0 - min_max_normalize(cost_ratio, config.cost_ratio_range)
    components["cost_contribution"] = w.cost_weight * cost_quality

    # --- Regime compatibility ---
    components["regime_contribution"] = w.regime_weight * candidate.regime_compatibility
    if candidate.regime_compatibility < config.min_regime_compatibility:
        soft_flags.append(SoftPreference.WEAK_REGIME_FIT)

    # --- Correlation penalty / diversification benefit ---
    corr, corr_state = correlation_provider.candidate_to_portfolio(
        candidate.symbol, portfolio_symbols
    )
    if corr_state == ValidationState.UNAVAILABLE:
        # No portfolio to correlate against yet -> max diversification benefit.
        diversification_benefit = 1.0
        correlation_penalty = 0.0
    elif corr_state == ValidationState.UNKNOWN:
        insufficient_data = True
        corr = config.unknown_correlation_assumption
        diversification_benefit = 1.0 - corr
        correlation_penalty = max(0.0, corr - config.high_correlation_threshold)
    else:
        diversification_benefit = 1.0 - (corr or 0.0)
        correlation_penalty = max(0.0, (corr or 0.0) - config.high_correlation_threshold)
        if (corr or 0.0) >= config.high_correlation_threshold:
            soft_flags.append(SoftPreference.HIGH_CORRELATION)

    components["diversification_contribution"] = w.diversification_weight * diversification_benefit
    components["correlation_penalty"] = -w.correlation_penalty_weight * correlation_penalty

    # --- Sector concentration ---
    sector_tracker = ExposureTracker(
        "sector", config.sector_concentration_threshold, config.sector_overlap_penalty
    )
    sector_penalty_raw = sector_tracker.penalty(portfolio_sectors, candidate.sector)
    components["sector_concentration_penalty"] = -w.sector_penalty_weight * sector_penalty_raw
    if sector_penalty_raw > 0:
        soft_flags.append(SoftPreference.SECTOR_CONCENTRATION)

    # --- Strategy concentration ---
    strategy_tracker = ExposureTracker(
        "strategy", config.strategy_concentration_threshold, config.strategy_overlap_penalty
    )
    strategy_key = candidate.strategy.value
    strategy_penalty_raw = strategy_tracker.penalty(portfolio_strategies, strategy_key)
    components["strategy_concentration_penalty"] = -w.strategy_penalty_weight * strategy_penalty_raw
    if strategy_penalty_raw > 0:
        soft_flags.append(SoftPreference.STRATEGY_CONCENTRATION)

    # --- Existing-position / duplicate overlap penalty ---
    overlap_penalty = 1.0 if candidate.symbol in portfolio_symbols else 0.0
    components["overlap_penalty"] = -w.overlap_penalty_weight * overlap_penalty

    total = sum(components.values())

    return CandidateScore(
        total=total,
        components=components,
        soft_flags=soft_flags,
        insufficient_data=insufficient_data,
    )
