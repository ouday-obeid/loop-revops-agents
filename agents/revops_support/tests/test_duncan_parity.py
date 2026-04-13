"""Tests for reports.duncan_parity."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text


@pytest.fixture
def _clear_audit_and_tasks():
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'revops_support'"))
        conn.execute(
            text("DELETE FROM tasks WHERE source LIKE 'duncan:%'")
        )
    yield
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'revops_support'"))
        conn.execute(
            text("DELETE FROM tasks WHERE source LIKE 'duncan:%'")
        )


def _insert_audit(action: str, when: datetime) -> None:
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log (agent_name, action, target, timestamp) "
                "VALUES ('revops_support', :a, 'test', :t)"
            ),
            {"a": action, "t": when},
        )


def _insert_duncan_task(category: str, source: str, when: datetime) -> None:
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks "
                "(agent_name, title, status, priority, category, source, created_at) "
                "VALUES ('duncan', :t, 'completed', 'medium', :c, :s, :w)"
            ),
            {"t": f"{category} work", "c": category, "s": source, "w": when},
        )


def test_collect_within_window(monkeypatch, _clear_audit_and_tasks, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.reports import duncan_parity

    week_of = date(2026, 4, 20)
    mid = datetime(2026, 4, 17, 12, 0, 0)
    outside_old = datetime(2026, 4, 10, 12, 0, 0)
    outside_new = datetime(2026, 4, 21, 12, 0, 0)

    _insert_audit("sf_update", mid)
    _insert_audit("sf_update", mid)
    _insert_audit("sf_bulk_update", mid)
    _insert_audit("sf_schema_modify", mid)
    _insert_audit("sf_update", outside_old)  # outside window
    _insert_audit("sf_update", outside_new)  # outside window

    _insert_duncan_task("sf_update", "duncan:retainer", mid)
    _insert_duncan_task("sf_update", "duncan:retainer", mid)
    _insert_duncan_task("sf_update", "duncan:retainer", mid)
    _insert_duncan_task("sf_bulk", "duncan:retainer", mid)
    _insert_duncan_task("sf_update", "duncan:retainer", outside_old)  # outside

    rows = duncan_parity.collect(week_of=week_of)
    as_map = {r.category: r for r in rows}

    assert as_map["sf_update"].agent_handled == 2
    assert as_map["sf_update"].duncan_billed == 3
    assert as_map["sf_update"].delta == -1

    assert as_map["sf_bulk"].agent_handled == 1
    assert as_map["sf_bulk"].duncan_billed == 1
    assert as_map["sf_bulk"].delta == 0

    assert as_map["sf_schema"].agent_handled == 1
    assert as_map["sf_schema"].duncan_billed == 0


def test_report_writes_csv_with_total(monkeypatch, _clear_audit_and_tasks, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.reports import duncan_parity

    week_of = date(2026, 4, 20)
    mid = datetime(2026, 4, 17, 12, 0, 0)
    _insert_audit("sf_update", mid)
    _insert_duncan_task("sf_update", "duncan:retainer", mid)

    path, rows = duncan_parity.report(week_of=week_of)
    assert path.exists()
    text_out = path.read_text()
    assert "category,agent_handled,duncan_billed,delta" in text_out
    assert "TOTAL" in text_out
    assert len(rows) >= 1


def test_empty_week_still_writes_header(monkeypatch, _clear_audit_and_tasks, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.reports import duncan_parity

    path, rows = duncan_parity.report(week_of=date(2026, 4, 20))
    assert path.exists()
    assert rows == []
    body = path.read_text()
    assert "category,agent_handled,duncan_billed,delta" in body
    # TOTAL row always present.
    assert "TOTAL,0,0,0" in body
