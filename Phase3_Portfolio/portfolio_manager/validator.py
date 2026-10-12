"""
Candidate validation.

Produces a ValidationState + reason codes per candidate. This is a
*structural/freshness/economic-validity* check only — it never re-predicts
or re-scores anything (DESIGN PART 4/12).
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Dict, List, Tuple

from .candidate import PortfolioCandidate
from .config import PortfolioManagerConfig
from .enums import RejectionReason, ValidationState
from .portfolio_state import MarketContext


@dataclass(frozen=True)
class ValidationResult:
    state: ValidationState
    reasons: List[RejectionReason]


def validate_candidate(
    candidate: PortfolioCandidate,
    context: MarketContext,
    config: PortfolioManagerConfig,
) -> ValidationResult:
    reasons: List[RejectionReason] = []

    if not candidate.is_structurally_complete():
        return ValidationResult(ValidationState.INVALID, [RejectionReason.MALFORMED_CANDIDATE])

    # Freshness (§28)
    age = (context.as_of - candidate.signal_timestamp).total_seconds()
    if age < 0:
        # Future-dated signal relative to as_of — this would be a look-ahead
        # violation (§37); treat as malformed/invalid, never "helpful".
        reasons.append(RejectionReason.MALFORMED_CANDIDATE)
    elif age > config.max_signal_age_seconds:
        reasons.append(RejectionReason.STALE_SIGNAL)

    # Economic validity
    if not candidate.economic_viability or candidate.expected_net_profit <= 0:
        reasons.append(RejectionReason.ECONOMICALLY_INVALID)

    if candidate.confidence < config.min_confidence:
        # Soft by default (see hard_constraints.py) but recorded here so
        # downstream scoring / explanation can reference it either way.
        reasons.append(RejectionReason.LOW_CONFIDENCE)

    if reasons and any(
        r
        in (
            RejectionReason.MALFORMED_CANDIDATE,
            RejectionReason.STALE_SIGNAL,
            RejectionReason.ECONOMICALLY_INVALID,
        )
        for r in reasons
    ):
        return ValidationResult(ValidationState.INVALID, reasons)

    return ValidationResult(ValidationState.VALID, reasons)


def validate_and_dedupe(
    candidates: List[PortfolioCandidate],
    context: MarketContext,
    config: PortfolioManagerConfig,
) -> Dict[str, ValidationResult]:
    """Validates every candidate, then flags duplicate symbols (keeping the
    highest opportunity_score among duplicates as the sole non-duplicate;
    the rest are marked INVALID/DUPLICATE_SYMBOL). Returns a map keyed by
    opportunity_id."""

    results: Dict[str, ValidationResult] = {}
    for c in candidates:
        results[c.opportunity_id] = validate_candidate(c, context, config)

    # Duplicate-symbol handling: among VALID candidates sharing a symbol,
    # keep only the one with the highest opportunity_score (deterministic
    # tie-break on opportunity_id string order — see ranking.py for the
    # full tie-break chain used at selection time).
    by_symbol: Dict[str, List[PortfolioCandidate]] = {}
    for c in candidates:
        if results[c.opportunity_id].state == ValidationState.VALID:
            by_symbol.setdefault(c.symbol, []).append(c)

    for symbol, group in by_symbol.items():
        if len(group) <= 1:
            continue
        group_sorted = sorted(
            group, key=lambda c: (-c.opportunity_score, c.opportunity_id)
        )
        for loser in group_sorted[1:]:
            results[loser.opportunity_id] = ValidationResult(
                ValidationState.INVALID, [RejectionReason.DUPLICATE_SYMBOL]
            )

    return results
