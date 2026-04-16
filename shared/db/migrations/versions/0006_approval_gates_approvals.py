"""Add approvals JSON column to approval_gates for dual-approval tiers.

Revision ID: 0006_approval_gates_approvals
Revises: 0005_slt_rep_config
Create Date: 2026-04-16

Rationale:
  Tiers with approver='jackie_and_o' (mark_churned) need to record both
  decisions before the gate flips to 'approved'. The single approved_by
  column can't carry that history. `approvals` is a JSON array of
  {approver, approved, decided_at} dicts; the second distinct approver
  with approved=true completes the gate.

Backfill: existing rows get NULL — decide_approval_gate treats NULL as
empty list, so old gates keep their current status until acted on.
"""
from shared.db.connection import get_engine
from sqlalchemy import text

revision = "0006_approval_gates_approvals"
down_revision = "0005_slt_rep_config"
branch_labels = None
depends_on = None


def _has_column(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)


def upgrade() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        if not _has_column(conn, "approval_gates", "approvals"):
            conn.execute(text("ALTER TABLE approval_gates ADD COLUMN approvals TEXT"))


def downgrade() -> None:
    # SQLite < 3.35 lacks DROP COLUMN; safe no-op for forward-only flow.
    pass
