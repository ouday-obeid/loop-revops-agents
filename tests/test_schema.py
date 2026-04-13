from sqlalchemy import text

from shared.db.connection import get_engine

EXPECTED = {"tasks", "agent_runs", "approval_gates", "audit_log", "rate_limits", "integration_health"}


def test_six_tables_exist():
    with get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    names = {r[0] for r in rows}
    assert EXPECTED.issubset(names), f"missing: {EXPECTED - names}"
