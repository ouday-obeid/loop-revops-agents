"""SLT Revenue Metrics specialist — adds pipeline_snapshots + forecast_history.

Revision ID: 0004_slt_revenue_metrics
Revises: 0003_cs_agent
Create Date: 2026-04-13

Both tables are append-only (snapshotter writes once per opp per day; backtest
writes once per run_date/horizon/weights_version). Indexes cover the hot paths:
  - date-bounded scans (morning snapshot fetch)
  - per-owner slicing (AE scorecards)
  - close-date + stage slicing (forecast cohorts)
  - single-opp lookup (mover detection)
"""
from shared.db.connection import get_engine
from sqlalchemy import text

revision = "0004_slt_revenue_metrics"
down_revision = "0003_cs_agent"
branch_labels = None
depends_on = None


_CREATE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS pipeline_snapshots (
        id            INTEGER PRIMARY KEY,
        snapshot_date DATE NOT NULL,
        opp_id        TEXT NOT NULL,
        stage         TEXT,
        amount        REAL,
        acv           REAL,
        close_date    DATE,
        owner_id      TEXT,
        owner_name    TEXT,
        account_id    TEXT,
        segment       TEXT,
        score         INTEGER,
        category      TEXT,
        probability   REAL,
        weighted_acv  REAL,
        metadata      TEXT,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (snapshot_date, opp_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snap_date  ON pipeline_snapshots(snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_snap_owner ON pipeline_snapshots(owner_id, snapshot_date)",
    "CREATE INDEX IF NOT EXISTS idx_snap_close ON pipeline_snapshots(close_date, stage)",
    "CREATE INDEX IF NOT EXISTS idx_snap_opp   ON pipeline_snapshots(opp_id)",
    """
    CREATE TABLE IF NOT EXISTS forecast_history (
        id                INTEGER PRIMARY KEY,
        run_date          DATE NOT NULL,
        horizon_quarter   TEXT NOT NULL,
        weights_version   TEXT,
        commit_amount     REAL,
        best_case_amount  REAL,
        weighted_amount   REAL,
        actuals_at_close  REAL,
        accuracy_pct      REAL,
        brier_score       REAL,
        deal_count        INTEGER,
        metadata          TEXT,
        created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (run_date, horizon_quarter, weights_version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fh_quarter ON forecast_history(horizon_quarter, run_date)",
]


_DROP_STATEMENTS = [
    "DROP INDEX IF EXISTS idx_fh_quarter",
    "DROP TABLE IF EXISTS forecast_history",
    "DROP INDEX IF EXISTS idx_snap_opp",
    "DROP INDEX IF EXISTS idx_snap_close",
    "DROP INDEX IF EXISTS idx_snap_owner",
    "DROP INDEX IF EXISTS idx_snap_date",
    "DROP TABLE IF EXISTS pipeline_snapshots",
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
