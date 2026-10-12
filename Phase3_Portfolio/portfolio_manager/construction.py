"""
Portfolio construction algorithm (spec §20-23, DESIGN PART 11).

CHOICE OF METHOD (spec §21):
We use **greedy constrained selection with re-scored marginal contribution**,
not integer/quadratic/mean-variance optimization.

Why greedy is appropriate here:
  - Scale: 10-50 candidates after FILTER2.0 (spec §35) — optimization
    machinery buys nothing at this scale.
  - Interpretability: every selection/rejection traces to one re-computed
    score at one step; a QP solution is far harder to explain per-candidate
    (spec §30 requires deterministic, explainable decisions).
  - Determinism: no solver tolerances/numerical-stability edge cases.
  - ₹1,000 account: capital is not the binding constraint at this stage
    anyway (sizing/feasibility are later components) — the binding question
    is *portfolio quality*, which the re-scored greedy loop captures because
    correlation/sector/strategy terms are recomputed against the
    portfolio-as-selected-so-far at every step (this is what distinguishes
    it from naive "sort by score and take the top N", spec §12/§18).

Limitations (must upgrade path):
  - Greedy can miss a globally superior combination that requires temporarily
    accepting a lower-scoring candidate (a true combinatorial trade-off).
  - Does not enforce a joint risk budget across all selections simultaneously
    (that's Portfolio Risk's job, later).
  - At larger candidate counts (100s+) or once real quantitative capital
    optimization is needed, upgrade to a constrained QP/mean-variance
    formulation with the same score components as objective terms.
"""

from dataclasses import dataclass
from typing import Dict, List, Tuple

from .candidate import PortfolioCandidate
from .config import PortfolioManagerConfig
from .correlation import CorrelationProvider
from .enums import CandidateSelectionStatus, RejectionReason, ValidationState
from .portfolio_state import MarketContext, PortfolioState
from .ranking import tie_break_key
from .scoring import CandidateScore, score_candidate
from .validator import ValidationResult


@dataclass
class ConstructionOutcome:
    selected_ids: List[str]
    rejected_ids: List[str]
    final_scores: Dict[str, CandidateScore]
    rejection_reasons: Dict[str, List[RejectionReason]]


def construct(
    candidates: List[PortfolioCandidate],
    validation: Dict[str, ValidationResult],
    portfolio_state: PortfolioState,
    correlation_provider: CorrelationProvider,
    context: MarketContext,
    config: PortfolioManagerConfig,
) -> ConstructionOutcome:
    selected_ids: List[str] = []
    rejected_ids: List[str] = []
    rejection_reasons: Dict[str, List[RejectionReason]] = {}
    final_scores: Dict[str, CandidateScore] = {}

    # Step 1-4: keep only VALID candidates; INVALID ones are rejected
    # outright with their validator reasons (hard constraints, spec §12).
    valid_candidates: List[PortfolioCandidate] = []
    for c in candidates:
        result = validation[c.opportunity_id]
        if result.state != ValidationState.VALID:
            rejected_ids.append(c.opportunity_id)
            rejection_reasons[c.opportunity_id] = result.reasons
        else:
            valid_candidates.append(c)

    portfolio_symbols = list(portfolio_state.open_symbols())
    portfolio_sectors = [p.sector for p in portfolio_state.open_positions]
    portfolio_strategies = [p.strategy.value for p in portfolio_state.open_positions]

    remaining = list(valid_candidates)
    step = 0
    while remaining and step < config.max_positions_preference + len(remaining):
        step += 1
        # Re-score every remaining candidate against the portfolio AS IT
        # WOULD STAND with selections made so far — this is what lets the
        # algorithm prefer A+C over A+B (spec §12/§38) instead of static
        # sort-by-score.
        scored: Dict[str, CandidateScore] = {}
        for c in remaining:
            scored[c.opportunity_id] = score_candidate(
                c,
                portfolio_symbols=portfolio_symbols,
                portfolio_sectors=portfolio_sectors,
                portfolio_strategies=portfolio_strategies,
                correlation_provider=correlation_provider,
                context=context,
                config=config,
            )
        for cid, s in scored.items():
            final_scores[cid] = s

        ordered = sorted(remaining, key=lambda c: tie_break_key(c, scored[c.opportunity_id].total))

        # Walk the ranked list and skip (outright reject) any candidate that
        # is near-duplicate exposure of something already selected, rather
        # than letting it consume a slot just because it clears the
        # absolute quality floor (spec §26 EXCESSIVE_PORTFOLIO_REDUNDANCY;
        # DESIGN PART 6 — the soft scoring penalty alone is not always
        # enough to keep a genuinely redundant candidate out).
        best = None
        for candidate in ordered:
            if portfolio_symbols:
                corr, corr_state = correlation_provider.candidate_to_portfolio(
                    candidate.symbol, portfolio_symbols
                )
                if (
                    corr_state == ValidationState.VALID
                    and corr is not None
                    and corr >= config.redundancy_correlation_threshold
                ):
                    rejected_ids.append(candidate.opportunity_id)
                    reasons = list(rejection_reasons.get(candidate.opportunity_id, []))
                    reasons.append(RejectionReason.EXCESSIVE_PORTFOLIO_REDUNDANCY)
                    rejection_reasons[candidate.opportunity_id] = reasons
                    remaining = [c for c in remaining if c.opportunity_id != candidate.opportunity_id]
                    continue
            best = candidate
            break

        if best is None:
            # Every remaining candidate was redundant; nothing left to add.
            break

        best_score = scored[best.opportunity_id].total

        # Absolute floor: never select something below the minimum bar,
        # regardless of how it compares to its peers (spec §10, §40 —
        # HOLD_CASH must be reachable even if all candidates "look okay"
        # relative to each other but not in absolute terms).
        if best_score < config.min_contribution_score_to_select:
            for c in remaining:
                rejected_ids.append(c.opportunity_id)
                reasons = list(rejection_reasons.get(c.opportunity_id, []))
                reasons.append(RejectionReason.LOW_MARGINAL_PORTFOLIO_VALUE)
                rejection_reasons[c.opportunity_id] = reasons
            break

        # Marginal-value floor once we already have selections: guards
        # against low-value pile-on (spec §20 step 10).
        if selected_ids and best_score < config.min_marginal_contribution:
            for c in remaining:
                rejected_ids.append(c.opportunity_id)
                reasons = list(rejection_reasons.get(c.opportunity_id, []))
                reasons.append(RejectionReason.EXCESSIVE_PORTFOLIO_REDUNDANCY)
                rejection_reasons[c.opportunity_id] = reasons
            break

        if len(selected_ids) >= config.max_positions_preference:
            for c in remaining:
                rejected_ids.append(c.opportunity_id)
                reasons = list(rejection_reasons.get(c.opportunity_id, []))
                reasons.append(RejectionReason.LOW_MARGINAL_PORTFOLIO_VALUE)
                rejection_reasons[c.opportunity_id] = reasons
            break

        selected_ids.append(best.opportunity_id)
        portfolio_symbols.append(best.symbol)
        portfolio_sectors.append(best.sector)
        portfolio_strategies.append(best.strategy.value)
        remaining = [c for c in remaining if c.opportunity_id != best.opportunity_id]

    return ConstructionOutcome(
        selected_ids=selected_ids,
        rejected_ids=rejected_ids,
        final_scores=final_scores,
        rejection_reasons=rejection_reasons,
    )
