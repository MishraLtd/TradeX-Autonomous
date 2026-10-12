"""
Deterministic tie-break ordering (spec §22).

Never relies on dict/set ordering or hashing for anything that affects the
decision — every sort here uses an explicit, documented key chain, with
opportunity_id as the final deterministic tiebreaker of last resort.
"""

from typing import Tuple

from .candidate import PortfolioCandidate


def tie_break_key(candidate: PortfolioCandidate, score: float) -> Tuple:
    """Descending-preference sort key. Sort candidates with
    `sorted(candidates, key=lambda c: tie_break_key(c, scores[c.id]))`.

    Order of precedence (spec §22):
      1. higher contribution score
      2. higher expected net return
      3. lower risk
      4. higher confidence
      5. lower expected_total_cost
      6. higher liquidity_score
      7. higher regime_compatibility
      8. opportunity_id (lexicographic, final deterministic tiebreak)
    """
    return (
        -score,
        -candidate.expected_net_return,
        candidate.expected_risk,
        -candidate.confidence,
        candidate.expected_total_cost,
        -candidate.liquidity_score,
        -candidate.regime_compatibility,
        candidate.opportunity_id,
    )
