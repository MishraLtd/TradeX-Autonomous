"""Builds the final PortfolioDecision from construction + existing-position
outcomes (spec §25-27, §30)."""

import uuid
from datetime import datetime
from typing import Dict, List

from .candidate import PortfolioCandidate
from .config import PortfolioManagerConfig
from .construction import ConstructionOutcome
from .decision import CandidateOutcome, ExistingPositionOutcome, PortfolioDecision
from .enums import DecisionType, ExistingPositionAction
from .portfolio_state import PortfolioState


def _explain_candidate(
    candidate: PortfolioCandidate,
    selected: bool,
    reasons: List,
    score_total: float,
) -> str:
    if selected:
        return (
            f"{candidate.symbol} selected: portfolio contribution score "
            f"{score_total:.3f} cleared the selection/marginal thresholds "
            f"after accounting for correlation, sector and strategy overlap "
            f"with the rest of the constructed portfolio."
        )
    reason_text = ", ".join(r.value for r in reasons) if reasons else "score below threshold"
    return f"{candidate.symbol} rejected: {reason_text}."


def build_decision(
    portfolio_state: PortfolioState,
    candidates_by_id: Dict[str, PortfolioCandidate],
    construction_outcome: ConstructionOutcome,
    existing_position_outcomes: List[ExistingPositionOutcome],
    snapshot_id: str,
    config: PortfolioManagerConfig,
    model_versions: Dict[str, str],
) -> PortfolioDecision:
    candidate_outcomes: List[CandidateOutcome] = []

    for cid, candidate in candidates_by_id.items():
        selected = cid in construction_outcome.selected_ids
        score_obj = construction_outcome.final_scores.get(cid)
        reasons = construction_outcome.rejection_reasons.get(cid, [])
        candidate_outcomes.append(
            CandidateOutcome(
                opportunity_id=cid,
                symbol=candidate.symbol,
                selected=selected,
                contribution_score=score_obj.total if score_obj else None,
                score_components=score_obj.components if score_obj else {},
                reason_codes=reasons,
                explanation=_explain_candidate(
                    candidate, selected, reasons, score_obj.total if score_obj else 0.0
                ),
            )
        )

    n_selected = len(construction_outcome.selected_ids)
    replacements = [
        o for o in existing_position_outcomes if o.action == ExistingPositionAction.REPLACE_CANDIDATE
    ]
    reviews = [
        o
        for o in existing_position_outcomes
        if o.action in (ExistingPositionAction.REDUCE_PRIORITY, ExistingPositionAction.EXIT_REVIEW)
    ]

    if replacements:
        decision_type = DecisionType.REPLACE_CANDIDATE
    elif reviews and n_selected == 0:
        decision_type = DecisionType.REDUCE_EXPOSURE_REVIEW
    elif n_selected == 0 and not portfolio_state.open_positions:
        decision_type = DecisionType.HOLD_CASH
    elif n_selected == 0:
        decision_type = DecisionType.MAINTAIN_PORTFOLIO
    elif not portfolio_state.open_positions:
        decision_type = DecisionType.NEW_PORTFOLIO
    else:
        decision_type = DecisionType.ADD_POSITIONS

    portfolio_score = None
    portfolio_score_components: Dict[str, float] = {}
    if n_selected:
        selected_scores = [
            construction_outcome.final_scores[cid] for cid in construction_outcome.selected_ids
        ]
        portfolio_score = sum(s.total for s in selected_scores) / len(selected_scores)
        keys = selected_scores[0].components.keys()
        portfolio_score_components = {
            k: sum(s.components[k] for s in selected_scores) / len(selected_scores) for k in keys
        }

    reason_codes = sorted(
        {r.value for outcomes in construction_outcome.rejection_reasons.values() for r in outcomes}
    )

    if decision_type == DecisionType.HOLD_CASH:
        explanation = (
            "No candidate cleared the minimum portfolio-contribution threshold "
            "(or all were invalid/redundant); holding cash rather than forcing "
            "capital deployment (spec §10)."
        )
    elif decision_type == DecisionType.MAINTAIN_PORTFOLIO:
        explanation = (
            "Existing positions are retained as-is; no candidate offered "
            "sufficient incremental portfolio value to justify a change."
        )
    else:
        selected_symbols = [candidates_by_id[cid].symbol for cid in construction_outcome.selected_ids]
        explanation = (
            f"Selected {selected_symbols} based on portfolio contribution "
            f"score after accounting for correlation, sector/strategy overlap "
            f"and existing-position interaction; remaining candidates were "
            f"rejected per their individual reason codes."
        )

    return PortfolioDecision(
        decision_id=str(uuid.uuid4()),
        timestamp=portfolio_state.timestamp,
        portfolio_id=portfolio_state.portfolio_id,
        decision_type=decision_type,
        selected_candidates=list(construction_outcome.selected_ids),
        rejected_candidates=list(construction_outcome.rejected_ids),
        existing_position_actions=existing_position_outcomes,
        candidate_outcomes=candidate_outcomes,
        portfolio_score=portfolio_score,
        portfolio_score_components=portfolio_score_components,
        portfolio_state_snapshot_id=snapshot_id,
        reason_codes=reason_codes,
        decision_explanation=explanation,
        model_versions=model_versions,
        portfolio_manager_version=config.version,
        decision_status="FINAL",
    )
