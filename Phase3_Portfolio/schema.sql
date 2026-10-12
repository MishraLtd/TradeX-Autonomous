-- TradeX Phase 3 — persistence for the integration layer.
--
-- Scope: the RUN and its decisions. Each component already ships its own
-- schema.sql for its internal audit rows (position_sizing/schema.sql,
-- capital_feasibility/schema.sql, portfolio_manager/schema.sql); those
-- are referenced here by audit id rather than duplicated.
--
-- SQLite dialect (TradeX's local store). Money is TEXT, holding the exact
-- Decimal string — never REAL, which would reintroduce the binary float
-- noise the adapters layer exists to keep out.

CREATE TABLE IF NOT EXISTS phase3_run (
    run_id                      TEXT PRIMARY KEY,
    portfolio_id                TEXT NOT NULL,
    run_timestamp               TEXT NOT NULL,     -- ISO-8601, the market_context as_of
    status                      TEXT NOT NULL,     -- Phase3Status
    decision_id                 TEXT,              -- portfolio_manager PortfolioDecision
    explanation                 TEXT,

    -- capital summary, as of the end of the run
    total_capital               TEXT NOT NULL,
    cash                        TEXT NOT NULL,
    reserve_required            TEXT NOT NULL,
    deployable_at_start         TEXT NOT NULL,
    capital_allocated           TEXT NOT NULL,
    deployable_remaining        TEXT NOT NULL,
    committed_capital_is_known  INTEGER NOT NULL,  -- 0 = unknown, NOT a verified zero

    -- risk before/after (full reports live in phase3_run_risk_report)
    risk_status_before          TEXT,
    risk_status_after           TEXT,
    risk_score_before           REAL,
    risk_score_after            REAL,

    phase3_version              TEXT NOT NULL,
    component_versions          TEXT,              -- JSON
    warnings                    TEXT,              -- JSON array
    created_at                  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_phase3_run_portfolio_time
    ON phase3_run (portfolio_id, run_timestamp DESC);


-- One row per emitted TradeInstruction. This is what the Execution Engine
-- reads, and what the Trade Journal later joins fills back onto.
CREATE TABLE IF NOT EXISTS phase3_instruction (
    instruction_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT NOT NULL REFERENCES phase3_run(run_id),
    opportunity_id              TEXT NOT NULL,
    position_id                 TEXT NOT NULL,

    symbol                      TEXT NOT NULL,
    exchange                    TEXT NOT NULL,
    sector                      TEXT,
    trade_type                  TEXT NOT NULL,     -- INTRADAY | DELIVERY
    side                        TEXT NOT NULL,     -- BUY

    quantity                    INTEGER NOT NULL,
    entry_price                 TEXT NOT NULL,
    position_value              TEXT NOT NULL,
    estimated_entry_cost        TEXT NOT NULL,
    required_capital            TEXT NOT NULL,
    stop_loss                   TEXT,
    target                      TEXT,
    estimated_trade_risk        TEXT,

    sizing_status               TEXT NOT NULL,
    sizing_method               TEXT NOT NULL,
    sizing_confidence           TEXT NOT NULL,
    binding_constraints         TEXT,              -- JSON array

    feasibility_status          TEXT NOT NULL,
    max_affordable_quantity     INTEGER,

    risk_score_before           REAL,
    risk_score_after            REAL,
    risk_status_after           TEXT,
    risk_contribution_pct       REAL,

    allocation_status           TEXT NOT NULL,     -- ALLOCATED | ALLOCATED_REDUCED
    reduced_by_stage            TEXT,              -- which stage cut the size
    explanation                 TEXT,

    sizing_audit_id             TEXT,              -- -> position_sizing audit row
    feasibility_audit_id        TEXT,              -- -> capital_feasibility audit row
    model_versions              TEXT,              -- JSON
    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE (run_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS idx_phase3_instruction_symbol
    ON phase3_instruction (symbol, created_at DESC);


-- One row per PM-selected candidate, allocated or NOT. The rejection
-- trail is the point: without it, "why didn't we take that trade?" is
-- unanswerable after the fact.
CREATE TABLE IF NOT EXISTS phase3_outcome (
    outcome_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT NOT NULL REFERENCES phase3_run(run_id),
    opportunity_id              TEXT NOT NULL,
    symbol                      TEXT NOT NULL,

    status                      TEXT NOT NULL,     -- AllocationStatus
    terminal_stage              TEXT NOT NULL,     -- which component decided
    reason                      TEXT,

    requested_quantity          INTEGER NOT NULL DEFAULT 0,
    final_quantity              INTEGER NOT NULL DEFAULT 0,

    sizing_status               TEXT,
    feasibility_status          TEXT,
    new_risk_breaches           TEXT,              -- JSON array
    notes                       TEXT,              -- JSON array

    created_at                  TEXT NOT NULL DEFAULT (datetime('now')),

    UNIQUE (run_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS idx_phase3_outcome_status
    ON phase3_outcome (status, created_at DESC);


-- Full risk reports, kept separately because they are large and only read
-- during investigation. One row per run per side.
CREATE TABLE IF NOT EXISTS phase3_run_risk_report (
    run_id                      TEXT NOT NULL REFERENCES phase3_run(run_id),
    side                        TEXT NOT NULL,     -- BEFORE | AFTER
    report_json                 TEXT NOT NULL,     -- schemas.PortfolioRiskReport
    PRIMARY KEY (run_id, side)
);


-- Existing-position actions the Portfolio Manager returned. Phase 3 passes
-- these through without acting on them (see docs/INTEGRATION.md §5), so
-- they are recorded here for whatever component takes them up.
CREATE TABLE IF NOT EXISTS phase3_existing_position_action (
    action_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                      TEXT NOT NULL REFERENCES phase3_run(run_id),
    position_id                 TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    action                      TEXT NOT NULL,     -- ExistingPositionAction
    explanation                 TEXT,
    acted_on                    INTEGER NOT NULL DEFAULT 0
);
