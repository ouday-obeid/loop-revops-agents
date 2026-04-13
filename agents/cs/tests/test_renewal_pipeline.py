"""M4 — T-120 renewal pipeline tests.

Covers: window math, idempotency (SOQL pre-check + state double-guard),
stage fallback, provisional flagging, dry-run, governance gate produced.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.cs.renewal import pipeline
from shared.db.connection import get_engine


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_renewal_state"))
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:renewal_pipeline:%'"))
        conn.execute(text("DELETE FROM approval_gates WHERE agent_name = 'cs'"))
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'cs'"))


class FakeSf:
    """Programmable SF double. Preloaded: describe response + due opps + existing renewals."""

    def __init__(
        self,
        *,
        has_renewal_stage: bool = True,
        due_opps: list[dict[str, Any]] | None = None,
        existing_renewal_ids: dict[tuple[str, str], str] | None = None,
    ):
        self.has_renewal_stage = has_renewal_stage
        self.due_opps = due_opps or []
        self.existing = existing_renewal_ids or {}
        self.created: list[dict[str, Any]] = []

    def describe_sobject(self, name: str):
        picks = [
            {"value": "Qualification", "active": True},
            {"value": "Closed Won", "active": True},
        ]
        if self.has_renewal_stage:
            picks.append({"value": "Renewal Outreach", "active": True})
        return {"fields": [{"name": "StageName", "picklistValues": picks}]}

    def soql_query(self, q: str, **_):
        if "Opportunity" in q and "Zen_Contract_End_Date__c >=" in q:
            return {"records": self.due_opps}
        if "Type = 'Renewal'" in q:
            # Extract account + date from query string for lookup.
            aid = q.split("AccountId = '")[1].split("'")[0]
            date = q.split("Zen_Contract_End_Date__c = ")[1].split(" ")[0].rstrip("LIMIT").strip()
            key = (aid, date)
            if key in self.existing:
                return {"records": [{"Id": self.existing[key]}]}
            return {"records": []}
        return {"records": []}

    def create_record(self, sobject, fields, *, agent_name, approval_gate_id, **_):
        assert sobject == "Opportunity"
        assert approval_gate_id is not None  # governance gate required
        new_id = f"006NEW{len(self.created):03d}"
        self.created.append({**fields, "id": new_id, "gate_id": approval_gate_id})
        return {"id": new_id, "success": True}


def _make_opp(acct_id: str, end_days: int, *, opp_id: str = "006SRC1", name: str = "Acme"):
    end = (datetime.now(timezone.utc) + timedelta(days=end_days)).date().isoformat()
    return {
        "Id": opp_id,
        "AccountId": acct_id,
        "Account": {"Name": name},
        "OwnerId": "005OWN1",
        "Amount": 50000,
        "Zen_Contract_End_Date__c": end,
    }


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    yield
    _clear()


@pytest.mark.asyncio
async def test_creates_renewal_when_none_exists():
    opp = _make_opp("001A1", 120)
    sf = FakeSf(due_opps=[opp])
    counters = await pipeline.run_sweep(sf_mcp=sf)

    assert counters["candidates"] == 1
    assert counters["created"] == 1
    assert counters["skipped"] == 0
    assert len(sf.created) == 1
    created = sf.created[0]
    assert created["Type"] == "Renewal"
    assert created["AccountId"] == "001A1"
    assert created["StageName"] == "Renewal Outreach"
    assert created["Name"].startswith("Acme Renewal ")

    engine = get_engine()
    with engine.begin() as conn:
        state = conn.execute(text("SELECT opportunity_id, provisional FROM cs_renewal_state")).mappings().first()
    assert state["opportunity_id"] == created["id"]
    assert state["provisional"] == 0


@pytest.mark.asyncio
async def test_skips_when_renewal_already_exists():
    opp = _make_opp("001A1", 120)
    end = opp["Zen_Contract_End_Date__c"]
    sf = FakeSf(due_opps=[opp], existing_renewal_ids={("001A1", end): "006EXIST"})
    counters = await pipeline.run_sweep(sf_mcp=sf)

    assert counters["created"] == 0
    assert counters["skipped"] == 1
    assert sf.created == []

    # State row still persisted for the existing renewal as double-guard.
    engine = get_engine()
    with engine.begin() as conn:
        state = conn.execute(text("SELECT opportunity_id FROM cs_renewal_state")).mappings().first()
    assert state["opportunity_id"] == "006EXIST"


@pytest.mark.asyncio
async def test_sweep_is_idempotent_across_runs():
    opp = _make_opp("001A1", 120)
    sf = FakeSf(due_opps=[opp])
    await pipeline.run_sweep(sf_mcp=sf)
    # Wire the created renewal into the existing map for round 2.
    created = sf.created[0]
    end = opp["Zen_Contract_End_Date__c"]
    sf2 = FakeSf(
        due_opps=[opp],
        existing_renewal_ids={("001A1", end): created["id"]},
    )
    counters2 = await pipeline.run_sweep(sf_mcp=sf2)
    assert counters2["created"] == 0
    assert counters2["skipped"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM cs_renewal_state")).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_window_tolerance_catches_118_and_122():
    opps = [
        _make_opp("001LO", 118, opp_id="006LO"),
        _make_opp("001HI", 122, opp_id="006HI"),
    ]
    sf = FakeSf(due_opps=opps)
    counters = await pipeline.run_sweep(sf_mcp=sf)
    assert counters["candidates"] == 2
    assert counters["created"] == 2


@pytest.mark.asyncio
async def test_stage_fallback_marks_provisional_and_opens_task():
    opp = _make_opp("001A1", 120)
    sf = FakeSf(due_opps=[opp], has_renewal_stage=False)
    counters = await pipeline.run_sweep(sf_mcp=sf)

    assert counters["created"] == 1
    assert counters["provisional"] == 1
    created = sf.created[0]
    assert created["StageName"] == "Qualification"  # fallback = first active picklist value

    engine = get_engine()
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT title, priority FROM tasks WHERE source = 'cs:renewal_pipeline:missing_stage'")
        ).mappings().first()
        state = conn.execute(text("SELECT provisional FROM cs_renewal_state")).mappings().first()
    assert task is not None
    assert task["priority"] == "high"
    assert state["provisional"] == 1


@pytest.mark.asyncio
async def test_dry_run_creates_nothing():
    opp = _make_opp("001A1", 120)
    sf = FakeSf(due_opps=[opp])
    counters = await pipeline.run_sweep(sf_mcp=sf, dry_run=True)
    assert counters["created"] == 0
    assert sf.created == []
    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM cs_renewal_state")).scalar()
    assert count == 0


@pytest.mark.asyncio
async def test_approval_gate_is_created_and_self_approved():
    opp = _make_opp("001A1", 120)
    sf = FakeSf(due_opps=[opp])
    await pipeline.run_sweep(sf_mcp=sf)
    assert sf.created[0]["gate_id"] is not None

    engine = get_engine()
    with engine.begin() as conn:
        gate = conn.execute(
            text(
                """SELECT status, action_type, payload FROM approval_gates
                    WHERE agent_name = 'cs' ORDER BY id DESC LIMIT 1"""
            )
        ).mappings().first()
    assert gate["status"] == "approved"
    assert gate["action_type"] == "single_record_update"
    import json
    payload = json.loads(gate["payload"])
    assert payload["origin"] == "cs_renewal_create"
    assert payload["account_id"] == "001A1"


@pytest.mark.asyncio
async def test_error_on_single_opp_does_not_abort_sweep():
    good = _make_opp("001GOOD", 120, opp_id="006GOOD")
    bad = {"Id": "006BAD", "AccountId": None, "Zen_Contract_End_Date__c": None}
    sf = FakeSf(due_opps=[bad, good])
    counters = await pipeline.run_sweep(sf_mcp=sf)
    assert counters["candidates"] == 2
    assert counters["created"] == 1
    assert counters["skipped"] == 1
