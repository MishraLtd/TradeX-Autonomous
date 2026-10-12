-- TradeX Market Regime Model - PostgreSQL / Supabase schema (§36)
-- Kept intentionally minimal per spec instruction "do not create unnecessary tables".

CREATE TABLE IF NOT EXISTS market_regime_model_versions (
    id                  BIGSERIAL PRIMARY KEY,
    model_version       TEXT NOT NULL,
    feature_version     TEXT NOT NULL,
    config_version      TEXT NOT NULL,
    description         TEXT,
    training_period_start TIMESTAMPTZ,
    training_period_end   TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (model_version, feature_version, config_version)
);

CREATE TABLE IF NOT EXISTS market_regime_states (
    id                          BIGSERIAL PRIMARY KEY,
    ts                          TIMESTAMPTZ NOT NULL,
    symbol_scope                TEXT NOT NULL,           -- e.g. 'MARKET:NIFTY50'
    market_regime                TEXT NOT NULL,
    confidence                  DOUBLE PRECISION NOT NULL,
    short_term_regime            TEXT NOT NULL,
    medium_term_regime           TEXT NOT NULL,
    long_term_regime             TEXT NOT NULL,
    regime_alignment_score        DOUBLE PRECISION,
    regime_stability_score        DOUBLE PRECISION,
    regime_duration_bars          INTEGER,
    previous_regime              TEXT,
    transition_probability        DOUBLE PRECISION,
    transition_direction          TEXT,
    sector_regime                TEXT,
    stock_regime                 TEXT,
    cross_level_alignment_score   DOUBLE PRECISION,
    recommended_strategy          TEXT,
    position_multiplier          DOUBLE PRECISION,
    trade_permission             TEXT NOT NULL,
    data_freshness_ok             BOOLEAN NOT NULL,
    model_version                TEXT NOT NULL,
    feature_version              TEXT NOT NULL,
    config_version                TEXT NOT NULL,
    reason_codes                 TEXT[],
    probabilities_json            JSONB,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_regime_states_scope_ts ON market_regime_states (symbol_scope, ts DESC);
CREATE INDEX IF NOT EXISTS idx_regime_states_regime ON market_regime_states (market_regime);

CREATE TABLE IF NOT EXISTS market_regime_features (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL,
    symbol_scope    TEXT NOT NULL,
    timeframe       TEXT NOT NULL,
    feature_name    TEXT NOT NULL,
    feature_value   DOUBLE PRECISION,
    feature_version TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_regime_features_lookup
    ON market_regime_features (symbol_scope, timeframe, feature_name, ts DESC);

CREATE TABLE IF NOT EXISTS market_regime_transitions (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL,
    symbol_scope        TEXT NOT NULL,
    from_regime          TEXT NOT NULL,
    to_regime            TEXT NOT NULL,
    duration_bars_before  INTEGER,
    transition_probability DOUBLE PRECISION,
    model_version        TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_regime_transitions_scope_ts ON market_regime_transitions (symbol_scope, ts DESC);

CREATE TABLE IF NOT EXISTS strategy_regime_performance (
    id                  BIGSERIAL PRIMARY KEY,
    strategy            TEXT NOT NULL,
    regime              TEXT NOT NULL,
    period_start         TIMESTAMPTZ NOT NULL,
    period_end           TIMESTAMPTZ NOT NULL,
    n_trades            INTEGER NOT NULL,
    win_rate            DOUBLE PRECISION,
    expected_return       DOUBLE PRECISION,
    avg_win             DOUBLE PRECISION,
    avg_loss            DOUBLE PRECISION,
    max_drawdown         DOUBLE PRECISION,
    sharpe              DOUBLE PRECISION,
    confidence_interval_low  DOUBLE PRECISION,
    confidence_interval_high DOUBLE PRECISION,
    model_version        TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (strategy, regime, period_start, period_end, model_version)
);
CREATE INDEX IF NOT EXISTS idx_strategy_regime_perf ON strategy_regime_performance (strategy, regime);

CREATE TABLE IF NOT EXISTS market_regime_backtests (
    id                  BIGSERIAL PRIMARY KEY,
    run_name            TEXT NOT NULL,
    system_variant       TEXT NOT NULL,   -- 'baseline_no_regime' | 'regime_aware'
    period_start         TIMESTAMPTZ NOT NULL,
    period_end           TIMESTAMPTZ NOT NULL,
    cagr                DOUBLE PRECISION,
    sharpe              DOUBLE PRECISION,
    sortino             DOUBLE PRECISION,
    max_drawdown         DOUBLE PRECISION,
    win_rate            DOUBLE PRECISION,
    profit_factor        DOUBLE PRECISION,
    n_trades            INTEGER,
    turnover            DOUBLE PRECISION,
    avg_transaction_cost  DOUBLE PRECISION,
    model_version        TEXT,
    config_version        TEXT,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_backtests_variant ON market_regime_backtests (system_variant, period_start);
