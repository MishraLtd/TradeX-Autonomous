"""
Existing-position evaluation (spec §6, §11, §39).

Classifies each existing PortfolioPosition as KEEP / REPLACE_CANDIDATE /
REDUCE_PRIORITY / EXIT_REVIEW / NO_ACTION *without executing anything*.
Replacement is judged on normalized score, not raw return, so that
"B has a higher return than A" is never sufficient by itself (spec §39).
"""

from typing import Dict, List, Optional, Tuple

from .candidate import PortfolioCandidate
from .config import PortfolioManagerConfig
from .correlation import CorrelationProvider
from .decision import ExistingPositionOutcome
from .enums import ExistingPositionAction
from .normalization import min_max_normalize
from .portfolio_state import MarketContext, PortfolioPosition
from .scoring import CandidateScore, score_candidate


def _position_pseudo_score(
    position: PortfolioPosition, config: PortfolioManagerConfig
) -> float:
    """Approximate a position's current standing on the same normalized
    scale as candidate scores, using the fields available on an open
    position (expected_remaining_return / expected_risk / confidence).
    This is intentionally simpler than full candidate scoring (no cost or
    regime terms are re-derived for an open position) — it exists purely
    for like-for-like replacement comparisons, not for ranking positions
    against each other."""
    w = config.weights
    return_q = min_max_normalize(
        position.expected_remaining_return, config.expected_net_return_range
    )
    risk_q = min_max_normalize(position.expected_risk, config.expected_risk_range)
    conf_q = min_max_normalize(position.confidence, config.confidence_range)
    return (
        w.return_weight * return_q
        + w.risk_weight * (1.0 - risk_q)
        + w.confidence_weight * conf_q
    )


def evaluate_existing_positions(
    positions: List[PortfolioPosition],
    candidate_scores: Dict[str, CandidateScore],
    candidates_by_id: Dict[str, PortfolioCandidate],
    config: PortfolioManagerConfig,
) -> List[ExistingPositionOutcome]:
    outcomes: List[ExistingPositionOutcome] = []

    # Best remaining unselected-so-far candidate score per symbol group is
    # decided by the caller (construction.py) before this is called for the
    # "which candidate might replace this position" comparison; here we
    # just need the overall best candidate not already matching the symbol.
    best_candidate_id: Optional[str] = None
    best_candidate_score = float("-inf")
    for cid, cscore in candidate_scores.items():
        if cscore.total > best_candidate_score:
            best_candidate_score = cscore.total
            best_candidate_id = cid

    for position in positions:
        # Exact-symbol candidate present -> direct comparison.
        same_symbol_candidate_id = None
        for cid, cand in candidates_by_id.items():
            if cand.symbol == position.symbol:
                same_symbol_candidate_id = cid
                break

        pos_score = _position_pseudo_score(position, config)

        if same_symbol_candidate_id is not None:
            cand_score = candidate_scores.get(same_symbol_candidate_id)
            if cand_score and cand_score.total - pos_score >= config.min_replacement_improvement:
                outcomes.append(
                    ExistingPositionOutcome(
                        position_id=position.position_id,
                        symbol=position.symbol,
                        action=ExistingPositionAction.REPLACE_CANDIDATE,
                        explanation=(
                            f"Candidate {same_symbol_candidate_id} scores "
                            f"{cand_score.total:.3f} vs current position's "
                            f"{pos_score:.3f} (>= min_replacement_improvement="
                            f"{config.min_replacement_improvement})."
                        ),
                        replacement_candidate_id=same_symbol_candidate_id,
                    )
                )
                continue
            outcomes.append(
                ExistingPositionOutcome(
                    position_id=position.position_id,
                    symbol=position.symbol,
                    action=ExistingPositionAction.KEEP,
                    explanation="Same-symbol candidate does not clear the "
                    "replacement-improvement margin.",
                )
            )
            continue

        # No exact-symbol candidate. Consider whether a much stronger,
        # sufficiently diversifying candidate exists that materially
        # outperforms this position, flagging for human REDUCE/EXIT review
        # rather than any automatic action (spec §11: never auto-replace).
        if (
            best_candidate_id is not None
            and best_candidate_score - pos_score >= config.min_replacement_improvement * 1.5
        ):
            outcomes.append(
                ExistingPositionOutcome(
                    position_id=position.position_id,
                    symbol=position.symbol,
                    action=ExistingPositionAction.REDUCE_PRIORITY,
                    explanation=(
                        f"A materially stronger candidate ({best_candidate_id}, "
                        f"score {best_candidate_score:.3f}) exists relative to "
                        f"this position's score {pos_score:.3f}; flagged for "
                        f"review, not auto-replaced."
                    ),
                )
            )
            continue

        outcomes.append(
            ExistingPositionOutcome(
                position_id=position.position_id,
                symbol=position.symbol,
                action=ExistingPositionAction.KEEP,
                explanation="No candidate materially outperforms this position.",
            )
        )

    return outcomes
