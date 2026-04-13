"""M1 — UID resolver tests. Uses isolated SQLite from root conftest."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from agents.cs.health import uid_resolver
from shared.db.connection import get_engine


def test_resolve_external_id_present():
    assert uid_resolver.resolve({"externalId": "001ABC"}) == "001ABC"


def test_resolve_snake_case_fallback():
    assert uid_resolver.resolve({"external_id": "001ABC"}) == "001ABC"


def test_resolve_missing_returns_none():
    assert uid_resolver.resolve({"id": "v1", "name": "Acme"}) is None


def test_resolve_empty_string_returns_none():
    assert uid_resolver.resolve({"externalId": "   "}) is None


def test_resolve_strips_whitespace():
    assert uid_resolver.resolve({"externalId": "  001ABC  "}) == "001ABC"


def _clear_cs_tables():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE agent_name = 'revops_support'"))
        conn.execute(text("DELETE FROM integration_health WHERE integration LIKE 'vitally%'"))


def test_log_miss_creates_task():
    _clear_cs_tables()
    uid_resolver.log_miss({"id": "v99", "name": "Orphan"}, reason="no externalId")
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT title, agent_name, priority, source FROM tasks WHERE source = :s"),
            {"s": "cs:uid_resolver:v99"},
        ).mappings().first()
    assert row is not None
    assert row["agent_name"] == "revops_support"
    assert "Orphan" in row["title"]
    assert row["priority"] == "medium"


def test_log_miss_idempotent():
    _clear_cs_tables()
    uid_resolver.log_miss({"id": "v99", "name": "Orphan"})
    uid_resolver.log_miss({"id": "v99", "name": "Orphan"})
    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source = :s"),
            {"s": "cs:uid_resolver:v99"},
        ).scalar()
    assert count == 1


def test_record_match_rate_healthy():
    _clear_cs_tables()
    uid_resolver.record_match_rate(total=100, matched=98)
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT status, error_message FROM integration_health
                   WHERE integration = 'vitally_uid_resolution'
                   ORDER BY checked_at DESC LIMIT 1"""
            )
        ).mappings().first()
    assert row["status"] == "healthy"
    assert row["error_message"] is None


def test_record_match_rate_degraded():
    _clear_cs_tables()
    uid_resolver.record_match_rate(total=100, matched=80)
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT status, error_message FROM integration_health
                   WHERE integration = 'vitally_uid_resolution'
                   ORDER BY checked_at DESC LIMIT 1"""
            )
        ).mappings().first()
    assert row["status"] == "degraded"
    assert "80.0%" in row["error_message"]


def test_record_match_rate_zero_total_noops():
    _clear_cs_tables()
    uid_resolver.record_match_rate(total=0, matched=0)
    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM integration_health WHERE integration = 'vitally_uid_resolution'"
            )
        ).scalar()
    assert count == 0
