"""PortfolioDecision — the sole output type of construct_portfolio (spec §25)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .enums import DecisionType, ExistingPositionAction, RejectionReason


@dataclass(frozen=True)
class CandidateOutcome:
    """Per-candidate audit record: score breakdown + final status."""

    opportunity_id: str
    symbol: str
    selected: bool
    contribution_score: Optional[float]
    score_components: Dict[str, float]
    reason_codes: List[RejectionReason] = field(default_factory=list)
    explanation: str = ""


@dataclass(frozen=True)
class ExistingPositionOutcome:
    position_id: str
    symbol: str
    action: ExistingPositionAction
    explanation: str = ""
    replacement_candidate_id: Optional[str] = None


@dataclass(frozen=True)
class PortfolioDecision:
    decision_id: str
    timestamp: datetime
    portfolio_id: str

    decision_type: DecisionType

    selected_candidates: List[str]  # opportunity_ids
    rejected_candidates: List[str]  # opportunity_ids

    existing_position_actions: List[ExistingPositionOutcome]
    candidate_outcomes: List[CandidateOutcome]

    portfolio_score: Optional[float]
    portfolio_score_components: Dict[str, float]

    portfolio_state_snapshot_id: str

    reason_codes: List[str]
    decision_explanation: str

    model_versions: Dict[str, str]
    portfolio_manager_version: str

    decision_status: str = "FINAL"  # FINAL | DECISION_UNAVAILABLE
