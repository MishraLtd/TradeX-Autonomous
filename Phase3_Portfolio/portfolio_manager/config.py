"""
Configuration for the Portfolio Manager.

Per spec §46, no constant used in scoring/construction may be hard-coded
elsewhere — every tunable lives here, with a documented default and the
reasoning for it. Defaults are labelled "initial assumption" and are meant
to be revisited once TradeX has live/backtest data to calibrate against.
"""

from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class ScoreWeights:
    """Weights for PortfolioContributionScore (DESIGN PART 6).
    Not required to sum to 1 — they are relative importances; the resulting
    score is only ever used for ranking/thresholding, not as an absolute
    return figure. Initial assumption: return, risk and diversification are
    weighted highest because for a ₹1,000 account, concentrated single bad
    trades are more damaging than modest cost/regime mis-fits."""

    return_weight: float = 0.25
    risk_weight: float = 0.20
    confidence_weight: float = 0.10
    cost_weight: float = 0.10
    regime_weight: float = 0.10
    diversification_weight: float = 0.15
    correlation_penalty_weight: float = 0.15
    sector_penalty_weight: float = 0.08
    strategy_penalty_weight: float = 0.07
    overlap_penalty_weight: float = 0.10


@dataclass(frozen=True)
class PortfolioManagerConfig:
    version: str = "3.1.0-phase3a"

    # --- Signal freshness (§28) ---
    # Initial assumption: 15 minutes for intraday-grade signals on a
    # low-capital, low-frequency system; revisit once latency is measured.
    max_signal_age_seconds: int = 900

    # --- Normalization ranges (§19). Robust, documented, no magic numbers
    # inline elsewhere. Values outside range are clipped, not extrapolated.
    expected_net_return_range: "tuple[float, float]" = (-0.02, 0.05)  # -2%..5%
    expected_risk_range: "tuple[float, float]" = (0.0, 0.05)          # 0%..5%
    confidence_range: "tuple[float, float]" = (0.0, 1.0)
    cost_ratio_range: "tuple[float, float]" = (0.0, 0.5)              # cost/return

    # --- Correlation handling (§13, §8) ---
    high_correlation_threshold: float = 0.70
    # Initial assumption: treat unknown correlation as moderately risky
    # rather than 0 (never assume independence for unmeasured pairs).
    unknown_correlation_assumption: float = 0.50
    # A KNOWN correlation at/above this level to an already-selected symbol
    # makes a candidate EXCESSIVE_PORTFOLIO_REDUNDANCY outright during
    # construction — this is stronger than the soft scoring penalty above,
    # because near-duplicate exposure defeats the point of a second
    # position regardless of its own standalone quality (spec §26).
    redundancy_correlation_threshold: float = 0.85

    # --- Concentration thresholds (§14, §15) ---
    # Fraction of portfolio (by position count) in one sector/strategy above
    # which a concentration penalty/flag applies.
    sector_concentration_threshold: float = 0.60
    strategy_concentration_threshold: float = 0.60
    sector_overlap_penalty: float = 0.30
    strategy_overlap_penalty: float = 0.25

    # --- Regime compatibility (§16) ---
    min_regime_compatibility: float = 0.30  # below this = POOR_REGIME_COMPATIBILITY flag

    # --- Confidence / economic thresholds ---
    min_confidence: float = 0.40
    min_economic_viability: bool = True  # candidate.economic_viability must be True

    # --- Portfolio construction (§20, §22) ---
    max_positions_preference: int = 5  # soft cap; not a hard wall
    # Minimum contribution score required to add a candidate at all
    # (drives HOLD_CASH / NO_ACTION per §10, §40). Initial assumption: the
    # theoretical maximum score with zero penalties is ~0.9 (sum of the
    # positive weights); requiring ~35% of that reflects that a ₹1,000
    # account with high proportional transaction costs should only trade
    # genuinely attractive, risk-adjusted setups rather than marginal ones.
    min_contribution_score_to_select: float = 0.32
    # Minimum *marginal* improvement a candidate must add once the greedy
    # loop already has selections, to avoid low-value pile-on (§20 step 10).
    min_marginal_contribution: float = 0.08

    # --- Existing-position replacement (§39) ---
    # A candidate must beat an existing position's score by at least this
    # margin, net of correlation/overlap penalties, to be flagged as a
    # REPLACE_CANDIDATE suggestion (never auto-executed).
    min_replacement_improvement: float = 0.10

    weights: ScoreWeights = field(default_factory=ScoreWeights)
