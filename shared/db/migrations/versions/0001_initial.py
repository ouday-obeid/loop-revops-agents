"""Initial schema — delegates to shared/db/schema.sql.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-13
"""
from shared.db.connection import get_engine, init_schema
from sqlalchemy import text

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

TABLES = ["tasks", "agent_runs", "approval_gates", "audit_log", "rate_limits", "integration_health"]


def upgrade() -> None:
    init_schema()


def downgrade() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        for t in TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
