"""
Persistence (spec §40, §41). Ships two things:

1. `SCHEMA_SQL` — Postgres/Supabase DDL for the minimal table set needed
   for reproducibility and future model evaluation.
2. Lightweight `to_row()` / `from_row()` helpers that convert an
   OpportunityAssessment to/from a flat dict suitable for insertion,
   without hard-coupling this package to any particular DB client
   (matches spec §66 "create clean interfaces/mocks rather than coupling
   the model to unfinished systems" - TradeX's Supabase wiring lives
   elsewhere, e.g. via the n8n/Supabase MCP tools already used for
   other TradeX components).
"""

from __future__ import annotations

import json
from typing import Any, Dict

from .schemas import OpportunityAssessment

SCHEMA_SQL = """
-- Spec §40: minimal, non-redundant table set.

CREATE TABLE IF NOT EXISTS opportunity_evaluations (
    opportunity_id      UUID PRIMARY KEY,
    symbol              TEXT NOT NULL,
    trade_type          TEXT NOT NULL,
    strategy            TEXT NOT NULL,
    evaluated_at        TIMESTAMPTZ NOT NULL,
    candidate_timestamp TIMESTAMPTZ NOT NULL,
    eligibility_status  TEXT NOT NULL,
    opportunity_score   NUMERIC,
    score_band          TEXT,
    score_confidence    TEXT,
    score_uncertainty   NUMERIC,
    absolute_rank       INTEGER,
    relative_rank       INTEGER,
    expected_gross_return_pct NUMERIC,
    expected_net_return_pct   NUMERIC,
    expected_net_profit       NUMERIC,
    probability_of_profit     NUMERIC,
    expected_downside_pct     NUMERIC,
    capital_feasibility  BOOLEAN,
    economic_viability   BOOLEAN,
    model_version        TEXT NOT NULL,
    config_version        TEXT NOT NULL,
    input_hash            TEXT,
    input_versions_json    JSONB,
    input_snapshot_json    JSONB,   -- full candidate payload, for exact replay
    ranking_explanation    TEXT
);

CREATE INDEX IF NOT EXISTS idx_opp_eval_symbol_time ON opportunity_evaluations (symbol, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_opp_eval_score ON opportunity_evaluations (opportunity_score);

CREATE TABLE IF NOT EXISTS opportunity_score_components (
    opportunity_id       UUID REFERENCES opportunity_evaluations(opportunity_id),
    net_edge             NUMERIC,
    risk_efficiency      NUMERIC,
    regime_compatibility NUMERIC,
    prediction_reliability NUMERIC,
    liquidity_execution  NUMERIC,
    capital_efficiency   NUMERIC,
    holding_time_efficiency NUMERIC,
    cost_resilience      NUMERIC,
    tail_risk_multiplier NUMERIC,
    PRIMARY KEY (opportunity_id)
);

CREATE TABLE IF NOT EXISTS opportunity_scenarios (
    opportunity_id       UUID REFERENCES opportunity_evaluations(opportunity_id),
    optimistic_score     NUMERIC,
    base_score           NUMERIC,
    conservative_score   NUMERIC,
    scenario_resilience  NUMERIC,
    PRIMARY KEY (opportunity_id)
);

CREATE TABLE IF NOT EXISTS opportunity_gate_results (
    opportunity_id  UUID REFERENCES opportunity_evaluations(opportunity_id),
    gate_name       TEXT NOT NULL,
    passed          BOOLEAN NOT NULL,
    detail          TEXT,
    PRIMARY KEY (opportunity_id, gate_name)
);

CREATE TABLE IF NOT EXISTS opportunity_model_versions (
    model_version    TEXT PRIMARY KEY,
    config_version   TEXT NOT NULL,
    config_json      JSONB NOT NULL,
    activated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notes            TEXT
);

-- spec §56: realized-outcome feedback loop, joined back to the evaluation
-- that produced the trade, for calibration/drift analysis (spec §51-54).
CREATE TABLE IF NOT EXISTS opportunity_outcomes (
    opportunity_id             UUID REFERENCES opportunity_evaluations(opportunity_id) PRIMARY KEY,
    execution_price            NUMERIC,
    actual_cost                NUMERIC,
    actual_return_pct          NUMERIC,
    actual_net_return_pct      NUMERIC,
    actual_mae_pct             NUMERIC,
    actual_mfe_pct             NUMERIC,
    actual_holding_period_days NUMERIC,
    actual_regime              TEXT,
    closed_at                  TIMESTAMPTZ
);
"""


def to_row(assessment: OpportunityAssessment, candidate_snapshot: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Flattens the top-level `opportunity_evaluations` row. Component/
    scenario/gate sub-tables are written separately via the nested
    payload still present on the assessment object."""
    return {
        "opportunity_id": assessment.opportunity_id,
        "symbol": assessment.symbol,
        "trade_type": assessment.trade_type.value,
        "strategy": assessment.strategy,
        "evaluated_at": assessment.evaluated_at.isoformat(),
        "candidate_timestamp": assessment.timestamp.isoformat(),
        "eligibility_status": assessment.eligibility_status.value,
        "opportunity_score": str(assessment.opportunity_score) if assessment.opportunity_score is not None else None,
        "score_band": assessment.score_band,
        "score_confidence": assessment.score_confidence.value if assessment.score_confidence else None,
        "score_uncertainty": str(assessment.score_uncertainty) if assessment.score_uncertainty is not None else None,
        "absolute_rank": assessment.absolute_rank,
        "relative_rank": assessment.relative_rank,
        "expected_gross_return_pct": _s(assessment.expected_gross_return_pct),
        "expected_net_return_pct": _s(assessment.expected_net_return_pct),
        "expected_net_profit": _s(assessment.expected_net_profit),
        "probability_of_profit": _s(assessment.probability_of_profit),
        "expected_downside_pct": _s(assessment.expected_downside_pct),
        "capital_feasibility": assessment.capital_feasibility,
        "economic_viability": assessment.economic_viability,
        "model_version": assessment.model_version,
        "config_version": assessment.config_version,
        "input_hash": assessment.input_hash,
        "input_versions_json": json.dumps(assessment.input_versions),
        "input_snapshot_json": json.dumps(candidate_snapshot) if candidate_snapshot else None,
        "ranking_explanation": assessment.ranking_explanation,
    }


def _s(v):
    return str(v) if v is not None else None
