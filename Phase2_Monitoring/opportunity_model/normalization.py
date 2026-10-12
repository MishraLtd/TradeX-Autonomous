"""
Normalization methodology (spec §7, README §7).

Phase 1 uses **reference-range normalization**: a floor value maps to 0,
a target value maps to 100, linear in between, and values are clamped
outside that band. This is deliberately simple, deterministic, and fully
auditable - appropriate for a system with no accumulated trade history
yet to build stable empirical percentile bands from.

Phase 2 (documented, not yet activated) should replace these fixed
reference ranges with **historical percentile / rank normalization**
computed per-strategy on a rolling walk-forward window, once enough
realized trades exist (see config.py comments and README §24 "Cross-
Strategy Comparability"). The function signatures here are written so
that swap is a drop-in replacement (same input/output shape: raw value
-> Decimal in [0,100]).
"""

from __future__ import annotations

from decimal import Decimal


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def linear_normalize(
    value: Decimal, floor: Decimal, target: Decimal, invert: bool = False
) -> Decimal:
    """
    Map `value` linearly onto [0, 100] using `floor` -> 0 and `target` -> 100.

    If `invert` is True, the relationship is reversed (lower raw value is
    better - used e.g. for cost ratios where floor=1.0(worst) target=0.10(best)).
    Values beyond the target are capped at 100 (no false extra credit for
    "too good to be true" inputs - spec §20 caution against false precision).
    """
    if target == floor:
        raise ValueError("target and floor must differ for normalization")

    span = target - floor
    raw = (value - floor) / span * Decimal("100")
    score = clamp(raw, Decimal("0"), Decimal("100"))
    return score


def percentile_normalize_stub(value: Decimal, historical_distribution) -> Decimal:
    """
    Placeholder for Phase 2. Given a sorted historical distribution of the
    same metric (per-strategy, walk-forward, out-of-sample), return the
    percentile rank of `value` scaled to [0,100]. Not used in Phase 1.
    """
    raise NotImplementedError(
        "Phase 2 feature: activate once sufficient walk-forward trade "
        "history exists (see README.md §24)."
    )


def safe_ratio(numerator: Decimal, denominator: Decimal, epsilon: Decimal = Decimal("0.0001")) -> Decimal:
    """
    Ratio with an epsilon floor on the denominator to avoid divide-by-zero
    / exploding ratios when e.g. expected_downside is ~0. This is a
    deliberate numerical-stability choice, not an economic one - see
    README §10 for why we floor rather than special-case zero-risk trades.
    """
    denom = denominator if abs(denominator) > epsilon else (epsilon if denominator >= 0 else -epsilon)
    return numerator / denom
