"""
Public API (spec §16).

    construct_portfolio(portfolio_state, candidates, market_context, config)
        -> PortfolioDecision

This is the ONLY function external callers (live trading loop or
backtester) should invoke. It never calls Zerodha Kite, never places
orders, never re-predicts price/return/risk (spec §4, §49).
"""

import uuid
from datetime import datetime
from typing import Dict, List, Optional

from .audit import build_snapshot
from .candidate import PortfolioCandidate
from .config import PortfolioManagerConfig
from .construction import construct
from .correlation import CorrelationProvider, StaticCorrelationProvider
from .decision import ExistingPositionOutcome, PortfolioDecision
from .decision_builder import build_decision
from .enums import DecisionType
from .existing_positions import evaluate_existing_positions
from .portfolio_state import MarketContext, PortfolioState
from .validator import validate_and_dedupe


def _unavailable_decision(portfolio_state: PortfolioState, reason: str, config: PortfolioManagerConfig) -> PortfolioDecision:
    return PortfolioDecision(
        decision_id=str(uuid.uuid4()),
        timestamp=getattr(portfolio_state, "timestamp", datetime.utcnow()),
        portfolio_id=getattr(portfolio_state, "portfolio_id", "UNKNOWN"),
        decision_type=DecisionType.DECISION_UNAVAILABLE,
        selected_candidates=[],
        rejected_candidates=[],
        existing_position_actions=[],
        candidate_outcomes=[],
        portfolio_score=None,
        portfolio_score_components={},
        portfolio_state_snapshot_id="UNAVAILABLE",
        reason_codes=["INVALID_SYSTEM_STATE"],
        decision_explanation=reason,
        model_versions={},
        portfolio_manager_version=config.version,
        decision_status="DECISION_UNAVAILABLE",
    )


def construct_portfolio(
    portfolio_state: PortfolioState,
    candidates: List[PortfolioCandidate],
    market_context: MarketContext,
    config: Optional[PortfolioManagerConfig] = None,
    correlation_provider: Optional[CorrelationProvider] = None,
) -> PortfolioDecision:
    config = config or PortfolioManagerConfig()

    # Fail-closed (spec §44): never guess when foundational state is bad.
    if portfolio_state is None or not portfolio_state.is_valid():
        return _unavailable_decision(
            portfolio_state, "PortfolioState missing or invalid (system/portfolio status).", config
        )
    if candidates is None:
        return _unavailable_decision(portfolio_state, "Candidate batch is None.", config)

    correlation_provider = correlation_provider or StaticCorrelationProvider()

    # Point-in-time guard (spec §37): reject any candidate signalled after
    # the market context's as_of instant before anything else touches it.
    candidates = [c for c in candidates if c.signal_timestamp is not None]

    validation = validate_and_dedupe(candidates, market_context, config)
    candidates_by_id = {c.opportunity_id: c for c in candidates}

    construction_outcome = construct(
        candidates=candidates,
        validation=validation,
        portfolio_state=portfolio_state,
        correlation_provider=correlation_provider,
        context=market_context,
        config=config,
    )

    existing_position_outcomes: List[ExistingPositionOutcome] = evaluate_existing_positions(
        positions=portfolio_state.open_positions,
        candidate_scores=construction_outcome.final_scores,
        candidates_by_id=candidates_by_id,
        config=config,
    )

    snapshot = build_snapshot(portfolio_state, candidates, market_context)

    model_versions: Dict[str, str] = {"portfolio_manager_version": config.version}
    for c in candidates:
        for k, v in c.model_versions.items():
            model_versions.setdefault(k, v)

    return build_decision(
        portfolio_state=portfolio_state,
        candidates_by_id=candidates_by_id,
        construction_outcome=construction_outcome,
        existing_position_outcomes=existing_position_outcomes,
        snapshot_id=snapshot["snapshot_id"],
        config=config,
        model_versions=model_versions,
    )
