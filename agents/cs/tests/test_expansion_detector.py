"""M7 — Expansion signal detector tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from agents.cs.expansion import expansion_detector
from shared.db.connection import get_engine


class FakeSf:
    def __init__(self, locations=None, brands=None):
        self._loc, self._brand = locations or [], brands or []
    def soql_query(self, q, **_):
        if "Location__c" in q:
            return {"records": self._loc}
        if "Brand_Logo__c" in q:
            return {"records": self._brand}
        return {"records": []}


class FakeFireflies:
    def __init__(self, transcripts=None): self._t = transcripts or []
    def list_transcripts(self, **_): return self._t


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:expansion:%'"))


@pytest.fixture(autouse=True)
def _clean():
    _clear(); yield; _clear()


@pytest.mark.asyncio
async def test_keyword_in_call_opens_task():
    ff = FakeFireflies([
        {
            "id": "t1", "title": "Acme weekly", "summary": "Client wants to add locations next quarter",
            "account_id": "001ACME",
        }
    ])
    counters = await expansion_detector.run_sweep(sf_mcp=FakeSf(), fireflies_mcp=ff)
    assert counters["call_keyword"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT title, category, assignee FROM tasks WHERE source LIKE 'cs:expansion:%'")
        ).mappings().first()
    assert "add location" in task["title"]
    assert task["category"] == "expansion"
    assert task["assignee"] == "blaine"


@pytest.mark.asyncio
async def test_call_without_keyword_ignored():
    ff = FakeFireflies([
        {"id": "t1", "title": "Standard check-in", "summary": "Everything fine", "account_id": "001A"}
    ])
    counters = await expansion_detector.run_sweep(sf_mcp=FakeSf(), fireflies_mcp=ff)
    assert counters["call_keyword"] == 0


@pytest.mark.asyncio
async def test_call_without_account_id_ignored():
    ff = FakeFireflies([{"id": "t1", "summary": "wants to upgrade"}])
    counters = await expansion_detector.run_sweep(sf_mcp=FakeSf(), fireflies_mcp=ff)
    assert counters["call_keyword"] == 0


@pytest.mark.asyncio
async def test_location_growth_signal():
    sf = FakeSf(locations=[{"Account__c": "001A", "c": 3}])
    counters = await expansion_detector.run_sweep(sf_mcp=sf, fireflies_mcp=FakeFireflies())
    assert counters["location_growth"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT title FROM tasks WHERE source LIKE 'cs:expansion:%location_growth%'")
        ).mappings().first()
    assert "3 new locations" in task["title"]


@pytest.mark.asyncio
async def test_location_below_threshold_ignored():
    sf = FakeSf(locations=[{"Account__c": "001A", "c": 1}])
    counters = await expansion_detector.run_sweep(sf_mcp=sf, fireflies_mcp=FakeFireflies())
    assert counters["location_growth"] == 0


@pytest.mark.asyncio
async def test_brand_added_signal():
    sf = FakeSf(brands=[{"Account__c": "001A", "c": 1}])
    counters = await expansion_detector.run_sweep(sf_mcp=sf, fireflies_mcp=FakeFireflies())
    assert counters["brand_added"] == 1


@pytest.mark.asyncio
async def test_same_signal_deduped_within_day():
    ff = FakeFireflies([
        {"id": "t1", "summary": "add locations please", "account_id": "001A"}
    ])
    await expansion_detector.run_sweep(sf_mcp=FakeSf(), fireflies_mcp=ff)
    counters2 = await expansion_detector.run_sweep(sf_mcp=FakeSf(), fireflies_mcp=ff)
    assert counters2["call_keyword"] == 0
    assert counters2["deduped"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source LIKE 'cs:expansion:%'")
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_multiple_signals_across_sources_for_same_account():
    ff = FakeFireflies([{"id": "t1", "summary": "upsell opportunity", "account_id": "001A"}])
    sf = FakeSf(
        locations=[{"Account__c": "001A", "c": 3}],
        brands=[{"Account__c": "001A", "c": 2}],
    )
    counters = await expansion_detector.run_sweep(sf_mcp=sf, fireflies_mcp=ff)
    assert counters["call_keyword"] == 1
    assert counters["location_growth"] == 1
    assert counters["brand_added"] == 1
    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source LIKE 'cs:expansion:001A:%'")
        ).scalar()
    assert count == 3  # one per signal type
