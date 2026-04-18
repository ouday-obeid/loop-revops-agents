"""SLT Revenue Metrics — rep_forecasts table for per-rep, per-quarter submissions.

Revision ID: 0007_rep_forecasts
Revises: 0006_approval_gates_approvals
Create Date: 2026-04-17

Feeds the "Rep Submitted Forecast" column on the Rep Forecast sheet.
Populated via `@oo slt ingest-rep-forecast <csv>`. One row per
(owner_name, quarter); re-ingesting a quarter upserts, preserving
the original `created_at` via `submitted_at` update.
"""
from shared.db.connection import get_engine
from sqlalchemy import text


revision = "0007_rep_forecasts"
down_revision = "0006_approval_gates_approvals"
branch_labels = None
depends_on = None


_CREATE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS rep_forecasts (
        owner_name      TEXT NOT NULL,
        quarter         TEXT NOT NULL,
        commit_acv      REAL,
        best_case_acv   REAL,
        notes           TEXT,
        source          TEXT,
        submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (owner_name, quarter)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rep_forecasts_quarter ON rep_forecasts(quarter)",
]


_DROP_STATEMENTS = [
    "DROP INDEX IF EXISTS idx_rep_forecasts_quarter",
    "DROP TABLE IF EXISTS rep_forecasts",
]


def upgrade() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for stmt in _CREATE_STATEMENTS:
            conn.execute(text(stmt))


def downgrade() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for stmt in _DROP_STATEMENTS:
            conn.execute(text(stmt))
