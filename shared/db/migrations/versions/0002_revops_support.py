"""RevOps Support specialist — schema extensions.

Revision ID: 0002_revops_support
Revises: 0001_initial
Create Date: 2026-04-13

Adds:
  - approval_gates.cooldown_until TIMESTAMP          (for schema_delete dual-approval 24h wait)
  - approval_gates.parent_gate_id INTEGER            (links confirmation gate -> primary gate)
  - describe_cache(org_alias, sobject, describe_json, fetched_at UNIQUE(org_alias,sobject))

init_schema() handles fresh DBs (schema.sql has the new columns in-place).
For existing DBs with the Phase 0 approval_gates shape, we ALTER TABLE idempotently.
"""
from shared.db.connection import get_engine, init_schema
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

revision = "0002_revops_support"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


_ADD_COLUMNS = [
    ("approval_gates", "cooldown_until", "TIMESTAMP"),
    ("approval_gates", "parent_gate_id", "INTEGER"),
]


def _add_column_if_missing(conn, table: str, column: str, coltype: str) -> None:
    """ALTER TABLE ADD COLUMN that's idempotent across SQLite and Postgres.

    Also skips silently if the table doesn't exist — fresh DBs get the column
    from schema.sql directly, so ALTER is only needed on pre-existing shapes.
    """
    try:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"))
    except (OperationalError, ProgrammingError) as e:
        msg = str(e).lower()
        if (
            "duplicate column" in msg
            or "already exists" in msg
            or "no such table" in msg
            or "does not exist" in msg
        ):
            return
        raise


def upgrade() -> None:
    # ALTER first so pre-existing DBs gain the new columns before init_schema()
    # re-applies schema.sql (which contains indexes that reference those columns).
    # On fresh DBs the ALTERs no-op (table doesn't exist yet) and init_schema()
    # creates everything from scratch.
    engine = get_engine()
    with engine.begin() as conn:
        for table, column, coltype in _ADD_COLUMNS:
            _add_column_if_missing(conn, table, column, coltype)
    init_schema()


def downgrade() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS idx_describe_age"))
        conn.execute(text("DROP TABLE IF EXISTS describe_cache"))
        conn.execute(text("DROP INDEX IF EXISTS idx_gates_cooldown"))
        conn.execute(text("DROP INDEX IF EXISTS idx_gates_parent"))
        for table, column, _ in _ADD_COLUMNS:
            try:
                conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
            except (OperationalError, ProgrammingError):
                pass
