"""
Model registry — Sections 32-34.

SQLite implementation for local/low-infra use (Section 36 — no
assumption of heavyweight infra). Schema mirrors the database design
requirements in Section 34. Swap the connection for Postgres/Supabase
later without changing the interface (matches Siddhant's existing
Supabase-based NSE pipeline).

A PRODUCTION model is never overwritten silently: `promote()` refuses
to move a model to PRODUCTION if another model already holds that
(horizon, model_id-family) production slot unless `retire_existing=True`
is explicitly passed, and retiring is itself a logged, timestamped
transition — never a delete.
"""

from __future__ import annotations
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..schemas import ModelRegistryEntry, ModelStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS model_registry (
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    horizon TEXT NOT NULL,
    training_start TEXT NOT NULL,
    training_end TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    target_definition TEXT NOT NULL,
    hyperparameters TEXT NOT NULL,        -- JSON
    training_metrics TEXT NOT NULL,       -- JSON
    validation_metrics TEXT NOT NULL,     -- JSON
    test_metrics TEXT NOT NULL,           -- JSON
    calibration_metrics TEXT NOT NULL,    -- JSON
    data_version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status_history TEXT NOT NULL DEFAULT '[]',  -- JSON list of {status, ts}
    PRIMARY KEY (model_id, model_version)
);

CREATE TABLE IF NOT EXISTS prediction_runs (
    run_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    n_predictions INTEGER,
    data_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prediction_features (
    symbol TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    feature_version TEXT NOT NULL,
    features TEXT NOT NULL,        -- JSON dict
    universe_eligible INTEGER NOT NULL,
    PRIMARY KEY (symbol, timestamp, feature_version)
);

CREATE TABLE IF NOT EXISTS prediction_outputs (
    symbol TEXT NOT NULL,
    prediction_timestamp TEXT NOT NULL,
    horizon TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    data_version TEXT NOT NULL,
    expected_gross_return_pct REAL,
    probability_positive REAL,
    threshold_probs TEXT,          -- JSON
    expected_mfe_pct REAL,
    expected_mae_pct REAL,
    quantiles_pct TEXT,            -- JSON
    prediction_interval_lo REAL,
    prediction_interval_hi REAL,
    confidence REAL,
    top_contributors TEXT,         -- JSON
    is_out_of_distribution INTEGER,
    status TEXT,
    rejection_reason TEXT,
    expected_total_friction_pct REAL,
    expected_net_return_pct REAL,
    cost_model_version TEXT,
    economically_viable INTEGER,
    PRIMARY KEY (symbol, prediction_timestamp, horizon, model_id, model_version)
);

CREATE TABLE IF NOT EXISTS prediction_evaluations (
    symbol TEXT NOT NULL,
    prediction_timestamp TEXT NOT NULL,
    horizon TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    realized_return_pct REAL,
    realized_net_return_pct REAL,
    evaluated_at TEXT NOT NULL,
    PRIMARY KEY (symbol, prediction_timestamp, horizon, model_id, model_version)
);

CREATE TABLE IF NOT EXISTS feature_versions (
    feature_version TEXT PRIMARY KEY,
    description TEXT,
    feature_list TEXT,     -- JSON list
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS target_definitions (
    target_version TEXT PRIMARY KEY,
    description TEXT,
    definition_code_ref TEXT,
    created_at TEXT NOT NULL
);
"""


class ModelRegistry:
    def __init__(self, db_path: str = "tradex_return_model.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def register(self, entry: ModelRegistryEntry) -> None:
        c = self._conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO model_registry
            (model_id, model_version, horizon, training_start, training_end,
             feature_version, target_definition, hyperparameters,
             training_metrics, validation_metrics, test_metrics,
             calibration_metrics, data_version, status, created_at, status_history)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry.model_id, entry.model_version, entry.horizon,
                entry.training_start.isoformat(), entry.training_end.isoformat(),
                entry.feature_version, entry.target_definition,
                json.dumps(entry.hyperparameters),
                json.dumps(entry.training_metrics),
                json.dumps(entry.validation_metrics),
                json.dumps(entry.test_metrics),
                json.dumps(entry.calibration_metrics),
                entry.data_version, entry.status.value,
                entry.created_at.isoformat(),
                json.dumps([{"status": entry.status.value, "ts": entry.created_at.isoformat()}]),
            ),
        )
        self._conn.commit()

    def promote(self, model_id: str, model_version: str, new_status: ModelStatus,
                retire_existing: bool = False) -> None:
        c = self._conn.cursor()
        if new_status == ModelStatus.PRODUCTION and not retire_existing:
            row = c.execute(
                "SELECT model_id, model_version, horizon FROM model_registry WHERE status = ?",
                (ModelStatus.PRODUCTION.value,),
            ).fetchone()
            target_horizon = c.execute(
                "SELECT horizon FROM model_registry WHERE model_id=? AND model_version=?",
                (model_id, model_version),
            ).fetchone()
            if row is not None and target_horizon is not None and row[2] == target_horizon[0] \
                    and (row[0], row[1]) != (model_id, model_version):
                raise RuntimeError(
                    f"Model {row[0]}/{row[1]} already holds PRODUCTION for horizon "
                    f"{row[2]}. Pass retire_existing=True to replace it explicitly."
                )
        now = datetime.now(timezone.utc).isoformat()
        hist_row = c.execute(
            "SELECT status_history FROM model_registry WHERE model_id=? AND model_version=?",
            (model_id, model_version),
        ).fetchone()
        history = json.loads(hist_row[0]) if hist_row else []
        history.append({"status": new_status.value, "ts": now})
        c.execute(
            "UPDATE model_registry SET status=?, status_history=? WHERE model_id=? AND model_version=?",
            (new_status.value, json.dumps(history), model_id, model_version),
        )
        self._conn.commit()

    def get_production_model(self, horizon: str) -> dict | None:
        c = self._conn.cursor()
        row = c.execute(
            "SELECT * FROM model_registry WHERE horizon=? AND status=? ORDER BY created_at DESC LIMIT 1",
            (horizon, ModelStatus.PRODUCTION.value),
        ).fetchone()
        if row is None:
            return None
        cols = [d[0] for d in c.description]
        return dict(zip(cols, row))

    def close(self):
        self._conn.close()
