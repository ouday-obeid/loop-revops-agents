"""Tests for knowledge_refresh.gap_surfacer."""
from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.fixture
def _clear_tasks():
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE agent_name = 'revops_support'"))
    yield
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE agent_name = 'revops_support'"))


def test_record_gap_creates_new_task(_clear_tasks):
    from agents.revops_support.knowledge_refresh import gap_surfacer
    from shared.db.connection import get_engine

    rec = gap_surfacer.record_gap("Missing X", "desc for X")
    assert rec.created is True
    assert rec.task_id > 0

    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT title, category, status, priority, source FROM tasks WHERE id = :i"),
            {"i": rec.task_id},
        ).fetchone()
    assert row[0] == "Missing X"
    assert row[1] == "knowledge_gap"
    assert row[2] == "pending"
    assert row[3] == "medium"
    assert row[4] == gap_surfacer.DEFAULT_SOURCE


def test_record_gap_dedupes_pending(_clear_tasks):
    from agents.revops_support.knowledge_refresh import gap_surfacer

    r1 = gap_surfacer.record_gap("Dup gap", "first")
    r2 = gap_surfacer.record_gap("Dup gap", "second")
    assert r1.created is True
    assert r2.created is False
    assert r1.task_id == r2.task_id


def test_record_gap_reopens_after_completion(_clear_tasks):
    from agents.revops_support.knowledge_refresh import gap_surfacer
    from shared.db.connection import get_engine

    r1 = gap_surfacer.record_gap("Transient gap", "desc")
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE tasks SET status = 'completed' WHERE id = :i"),
            {"i": r1.task_id},
        )
    r2 = gap_surfacer.record_gap("Transient gap", "desc")
    assert r2.created is True
    assert r2.task_id != r1.task_id


def test_scan_custom_objects_records_missing(monkeypatch, _clear_tasks):
    from agents.revops_support.knowledge_refresh import gap_surfacer

    fake_sobjects = {
        "result": [
            {"name": "Territory__c", "custom": True, "label": "Territory"},
            {"name": "TLO__c", "custom": True, "label": "TLO"},
            {"name": "Account", "custom": False, "label": "Account"},
        ]
    }
    monkeypatch.setattr(
        gap_surfacer.salesforce_mcp,
        "_sf",
        lambda *a, **kw: fake_sobjects if a[:2] == ("sobject", "list") else {},
    )
    # Pretend the knowledge base already covers TLO__c but not Territory__c.
    monkeypatch.setattr(
        gap_surfacer,
        "_object_has_knowledge",
        lambda name, corpus=gap_surfacer.DEFAULT_CORPUS, min_hits=1: name == "TLO__c",
    )

    gaps = gap_surfacer.scan_custom_objects()
    assert len(gaps) == 1
    assert "Territory__c" in gaps[0].title
    assert gaps[0].created is True

    # Second call should not double-report.
    gaps2 = gap_surfacer.scan_custom_objects()
    assert len(gaps2) == 1
    assert gaps2[0].created is False
