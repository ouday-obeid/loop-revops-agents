"""CS Agent — schema additions.

Revision ID: 0003_cs_agent
Revises: 0002_revops_support
Create Date: 2026-04-13

Adds four tables owned by the CS agent:
  - cs_account_health            (current Vitally snapshot per SF Account)
  - cs_account_health_history    (append-only trend data for drop detection)
  - cs_churn_risk                (daily scored cohort; UNIQUE(account_id, created_at))
  - cs_renewal_state             (double-guard idempotency for T-120 opp creation)

Fresh DBs get these from schema.sql via init_schema(). Existing DBs gain them
via init_schema() which is idempotent (all CREATEs use IF NOT EXISTS).
"""
from shared.db.connection import get_engine, init_schema
from sqlalchemy import text

revision = "0003_cs_agent"
down_revision = "0002_revops_support"
branch_labels = None
depends_on = None


def upgrade() -> None:
    init_schema()


def downgrade() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for ddl in (
            "DROP INDEX IF EXISTS idx_cs_renewal_end",
            "DROP INDEX IF EXISTS idx_cs_renewal_account",
            "DROP TABLE IF EXISTS cs_renewal_state",
            "DROP INDEX IF EXISTS idx_cs_risk_account",
            "DROP INDEX IF EXISTS idx_cs_risk_tier",
            "DROP TABLE IF EXISTS cs_churn_risk",
            "DROP INDEX IF EXISTS idx_cs_health_hist",
            "DROP TABLE IF EXISTS cs_account_health_history",
            "DROP INDEX IF EXISTS idx_cs_health_vitally",
            "DROP INDEX IF EXISTS idx_cs_health_checked",
            "DROP TABLE IF EXISTS cs_account_health",
        ):
            conn.execute(text(ddl))
