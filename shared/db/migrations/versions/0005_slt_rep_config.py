"""SLT Revenue Metrics — rep_config table for quotas + attainment calibrations.

Revision ID: 0005_slt_rep_config
Revises: 0004_slt_revenue_metrics
Create Date: 2026-04-13

Rationale (plan §Known Gaps / Tracked TODOs #2):
  LUCID hardcodes the rep roster in code. Agent 6 keeps it in DB so Hutch can
  tune quotas + attainment floors without a deploy. One row per AE / SDR,
  keyed on owner_name (matching SF `Owner.Name`).

Effective dating is deliberately simple — a single current quota + a single
attainment floor per rep. Per-quarter overrides (hiring ramps, maternity
coverage) live in `metadata` JSON until volume justifies a dedicated table.
"""
from shared.db.connection import get_engine
from sqlalchemy import text

revision = "0005_slt_rep_config"
down_revision = "0004_slt_revenue_metrics"
branch_labels = None
depends_on = None


_CREATE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS rep_config (
        owner_name           TEXT PRIMARY KEY,
        role                 TEXT,                 -- 'AE' | 'SDR' | 'MANAGER'
        team                 TEXT,                 -- 'ENT' | 'MM' | 'SMB' | 'SDR'
        quarterly_quota      REAL,
        annual_quota         REAL,
        attainment_floor_pct REAL DEFAULT 0.70,    -- below → REP_RISK flag eligible
        active               INTEGER DEFAULT 1,
        metadata             TEXT,                 -- JSON: notes, effective_quarter, ramp, …
        updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_rep_config_role ON rep_config(role, active)",
    "CREATE INDEX IF NOT EXISTS idx_rep_config_team ON rep_config(team, active)",
]


_DROP_STATEMENTS = [
    "DROP INDEX IF EXISTS idx_rep_config_team",
    "DROP INDEX IF EXISTS idx_rep_config_role",
    "DROP TABLE IF EXISTS rep_config",
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
