"""
Lightweight persistence helpers. This module builds parameterized SQL
against db/schema.sql; it deliberately does NOT open a DB connection
itself (Supabase/psycopg2 client is TradeX's concern, not this package's -
keeps market_regime dependency-free per §46). Wire the returned
(sql, params) tuple into whatever client TradeX already uses for Postgres.
"""

from __future__ import annotations
from .schemas import RegimeState
import json


def insert_regime_state_sql(state: RegimeState):
    sql = """
    INSERT INTO market_regime_states (
        ts, symbol_scope, market_regime, confidence,
        short_term_regime, medium_term_regime, long_term_regime,
        regime_alignment_score, regime_stability_score, regime_duration_bars,
        previous_regime, transition_probability, transition_direction,
        sector_regime, stock_regime, cross_level_alignment_score,
        recommended_strategy, position_multiplier, trade_permission,
        data_freshness_ok, model_version, feature_version, config_version,
        reason_codes, probabilities_json
    ) VALUES (
        to_timestamp(%s), %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s,
        %s, %s, %s, %s,
        %s, %s
    )
    """
    params = (
        state.timestamp, state.symbol_scope, state.market_regime.value,
        state.market_regime_confidence,
        state.short_term_regime.value, state.medium_term_regime.value, state.long_term_regime.value,
        state.regime_alignment_score, state.regime_stability_score, state.regime_duration_bars,
        state.previous_regime.value if state.previous_regime else None,
        state.transition_probability, state.transition_direction.value,
        state.sector_regime.value if state.sector_regime else None,
        state.stock_regime.value if state.stock_regime else None,
        state.cross_level_alignment_score,
        state.recommended_strategy, state.recommended_position_multiplier,
        state.trade_permission.value,
        state.data_freshness_ok, state.model_version, state.feature_version, state.config_version,
        list(state.reason_codes), json.dumps(state.market_regime_probabilities.values),
    )
    return sql, params
