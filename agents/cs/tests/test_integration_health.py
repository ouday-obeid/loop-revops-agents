"""M2 — CS integration health poller tests. Offline via fakes."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from agents.cs import integration_health
from shared.db.connection import get_engine


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM integration_health WHERE integration LIKE 'cs_%'"))
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:integration_health:%'"))
        conn.execute(text("DELETE FROM cs_account_health"))


class _FakeVitallyOk:
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return None
    async def list_accounts(self, *, limit: int = 100): return {"results": [], "next": None}


class _FakeVitallyDown:
    def __init__(self, *_, **__): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *_): return None
    async def list_accounts(self, *, limit: int = 100): raise RuntimeError("boom 500")


class _FakeFirefliesOk:
    def list_transcripts(self, **_): return [{"id": "t1"}]


class _FakeFirefliesDown:
    def list_transcripts(self, **_): raise RuntimeError("auth failed")


class _FakeSfOk:
    def soql_query(self, q, **_):
        if "Momentum" in q:
            return {"totalSize": 1, "records": [{"c": 1}]}
        return {"totalSize": 1, "records": [{"c": 1}]}


class _FakeSfMomentumSilent:
    def soql_query(self, q, **_):
        if "Momentum" in q:
            return {"totalSize": 0, "records": [{"c": 0}]}
        return {"totalSize": 1, "records": [{"c": 1}]}


class _FakeSfDown:
    def soql_query(self, *_a, **_kw): raise RuntimeError("sf unreachable")


@pytest.fixture(autouse=True)
def _set_keys(monkeypatch):
    monkeypatch.setenv("VITALLY_API_KEY", "test-key")
    monkeypatch.setenv("FIREFLIES_API_KEY", "test-key")
    _clear()


@pytest.mark.asyncio
async def test_poll_all_healthy_records_five_rows():
    result = await integration_health.poll(
        vitally_client_factory=_FakeVitallyOk,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfOk(),
    )
    assert set(result.keys()) == {
        "cs_vitally", "cs_fireflies", "cs_salesforce", "cs_momentum_sync", "cs_nps_freshness",
    }
    # NPS degraded (no cs_account_health rows); others healthy
    assert result["cs_vitally"]["status"] == "healthy"
    assert result["cs_fireflies"]["status"] == "healthy"
    assert result["cs_salesforce"]["status"] == "healthy"
    assert result["cs_momentum_sync"]["status"] == "healthy"
    assert result["cs_nps_freshness"]["status"] == "degraded"

    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM integration_health WHERE integration LIKE 'cs_%'")
        ).scalar()
    assert count == 5


@pytest.mark.asyncio
async def test_vitally_down_creates_task_on_transition():
    # Seed a prior healthy row so the probe transitions healthy -> down.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO integration_health (integration, status, checked_at)
                   VALUES ('cs_vitally', 'healthy', :t)"""
            ),
            {"t": datetime.now(timezone.utc) - timedelta(minutes=30)},
        )

    result = await integration_health.poll(
        vitally_client_factory=_FakeVitallyDown,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfOk(),
    )
    assert result["cs_vitally"]["status"] == "down"
    assert result["cs_vitally"]["changed_from"] == "healthy"

    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT title, priority FROM tasks WHERE source = 'cs:integration_health:cs_vitally'")
        ).mappings().first()
    assert task is not None
    assert "down" in task["title"]
    assert task["priority"] == "high"


@pytest.mark.asyncio
async def test_fireflies_missing_key_is_degraded(monkeypatch):
    monkeypatch.setenv("FIREFLIES_API_KEY", "REPLACE")
    result = await integration_health.poll(
        vitally_client_factory=_FakeVitallyOk,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfOk(),
    )
    assert result["cs_fireflies"]["status"] == "degraded"
    assert "not configured" in result["cs_fireflies"]["error"]


@pytest.mark.asyncio
async def test_momentum_silent_break_detected():
    result = await integration_health.poll(
        vitally_client_factory=_FakeVitallyOk,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfMomentumSilent(),
    )
    assert result["cs_momentum_sync"]["status"] == "down"
    assert "zero Momentum" in result["cs_momentum_sync"]["error"]


@pytest.mark.asyncio
async def test_sf_down_marks_both_sf_and_momentum():
    result = await integration_health.poll(
        vitally_client_factory=_FakeVitallyOk,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfDown(),
    )
    assert result["cs_salesforce"]["status"] == "down"
    # momentum probe catches the same SF exception as a degraded check-failure
    assert result["cs_momentum_sync"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_nps_freshness_healthy_when_recent_rows_present():
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        # 5 accounts: 3 with fresh NPS, 2 stale -> 60% >= 40% threshold
        for i, days in enumerate([1, 5, 20, 40, 90]):
            conn.execute(
                text(
                    """INSERT INTO cs_account_health (account_id, score, nps_score, nps_at, checked_at)
                       VALUES (:a, 80, 9, :n, :c)"""
                ),
                {"a": f"A{i}", "n": now - timedelta(days=days), "c": now},
            )
    result = await integration_health.poll(
        vitally_client_factory=_FakeVitallyOk,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfOk(),
    )
    assert result["cs_nps_freshness"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_status_change_does_not_double_open_task():
    # Two consecutive down polls: only one task should exist.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO integration_health (integration, status, checked_at)
                   VALUES ('cs_vitally', 'healthy', :t)"""
            ),
            {"t": datetime.now(timezone.utc) - timedelta(minutes=30)},
        )
    await integration_health.poll(
        vitally_client_factory=_FakeVitallyDown,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfOk(),
    )
    await integration_health.poll(
        vitally_client_factory=_FakeVitallyDown,
        fireflies_mcp=_FakeFirefliesOk(),
        sf_mcp=_FakeSfOk(),
    )
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source = 'cs:integration_health:cs_vitally'")
        ).scalar()
    assert count == 1
