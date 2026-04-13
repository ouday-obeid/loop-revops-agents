"""Closed-won poller — strategy selection, idempotency, error handling."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from agents.onboarding import closed_won_poller as poller
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _reset_tasks_table():
    """Each test starts without the schema-gap task seeded."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM tasks WHERE source = :s"),
                     {"s": poller._SCHEMA_GAP_TASK_SOURCE})
    yield


def test_detect_dedup_strategy_a_when_field_exists(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_soql(
        "Onboarding_Record_Created__c",
        {"records": [{"QualifiedApiName": "Onboarding_Record_Created__c"}]},
    )
    assert poller.detect_dedup_strategy() == "A"


def test_detect_dedup_strategy_b_when_field_missing(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_soql("Onboarding_Record_Created__c",
                                 {"records": [], "totalSize": 0})
    assert poller.detect_dedup_strategy() == "B"


def test_detect_dedup_strategy_b_when_probe_raises(monkeypatch):
    from shared.mcp import salesforce_mcp
    def boom(*a, **kw):
        raise RuntimeError("tooling flaky")
    monkeypatch.setattr(salesforce_mcp, "soql_query", boom)
    assert poller.detect_dedup_strategy() == "B"


@pytest.mark.asyncio
async def test_poll_creates_onboarding_once_per_opp(
    make_opp, fake_sf_monkeypatch, monkeypatch
):
    # Strategy A probe → field exists
    fake_sf_monkeypatch.set_soql("Onboarding_Record_Created__c",
                                 {"records": [{"QualifiedApiName": "x"}]})
    # Closed won SOQL
    opp = make_opp(opp_id="006_POLL_A")
    fake_sf_monkeypatch.set_soql(
        "StageName = 'Closed Won'",
        {"records": [opp], "totalSize": 1, "done": True},
    )
    # Belt-and-suspenders check → no existing Onboarding__c
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_POLL_A'",
        {"records": [], "totalSize": 0},
    )

    # Silence Slack from the creator.
    class Silent:
        def send(self, *a, **kw):
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())

    summary = await poller.poll()
    assert summary["strategy"] == "A"
    assert summary["candidates"] == 1
    assert summary["created"] == 1
    assert summary["skipped"] == 0
    assert not summary["errors"]
    # FakeSF recorded exactly one create_record call.
    assert len(fake_sf_monkeypatch.created) == 1


@pytest.mark.asyncio
async def test_poll_belt_and_suspenders_skips_existing(
    make_opp, fake_sf_monkeypatch, monkeypatch
):
    fake_sf_monkeypatch.set_soql("Onboarding_Record_Created__c",
                                 {"records": [{"QualifiedApiName": "x"}]})
    opp = make_opp(opp_id="006_EXIST")
    fake_sf_monkeypatch.set_soql(
        "StageName = 'Closed Won'",
        {"records": [opp], "totalSize": 1, "done": True},
    )
    # Existing Onboarding__c for this opp
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_EXIST'",
        {"records": [{"Id": "a01PRE_EXISTING"}], "totalSize": 1},
    )

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender",
                        lambda: type("S", (), {"send": lambda *a, **kw: None})())

    summary = await poller.poll()
    assert summary["skipped"] == 1
    assert summary["created"] == 0
    assert len(fake_sf_monkeypatch.created) == 0


@pytest.mark.asyncio
async def test_poll_strategy_b_filters_existing_in_bulk(
    make_opp, fake_sf_monkeypatch, monkeypatch
):
    # Probe returns no field → Strategy B
    fake_sf_monkeypatch.set_soql("Onboarding_Record_Created__c",
                                 {"records": [], "totalSize": 0})
    # Closed won list: two candidates
    a = make_opp(opp_id="006_B_A")
    b = make_opp(opp_id="006_B_B")
    fake_sf_monkeypatch.set_soql(
        "StageName = 'Closed Won'",
        {"records": [a, b], "totalSize": 2, "done": True},
    )
    # Strategy B existence query: 006_B_A already has an Onboarding__c
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c IN",
        {"records": [{"Id": "a01EX", "Opportunity__c": "006_B_A"}]},
    )
    # Belt-and-suspenders check for 006_B_B
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_B_B'",
        {"records": [], "totalSize": 0},
    )

    class Silent:
        def send(self, *a, **kw):
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())

    summary = await poller.poll()
    assert summary["strategy"] == "B"
    # Strategy B filtered 006_B_A → only 006_B_B is a candidate
    assert summary["candidates"] == 1
    assert summary["created"] == 1

    # Schema-gap task seeded for Agent 5
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql_text("SELECT agent_name, category FROM tasks WHERE source = :s"),
            {"s": poller._SCHEMA_GAP_TASK_SOURCE},
        ).fetchone()
    assert row is not None
    assert row[0] == "revops_support"
    assert row[1] == "sf_schema_gap"


@pytest.mark.asyncio
async def test_poll_idempotent_across_two_ticks(
    make_opp, fake_sf_monkeypatch, monkeypatch
):
    """Second tick with the same opp + existing Onboarding__c → no second create."""
    fake_sf_monkeypatch.set_soql("Onboarding_Record_Created__c",
                                 {"records": [{"QualifiedApiName": "x"}]})
    opp = make_opp(opp_id="006_IDEMP")
    fake_sf_monkeypatch.set_soql(
        "StageName = 'Closed Won'",
        {"records": [opp], "totalSize": 1, "done": True},
    )
    # Initially no Onboarding__c
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_IDEMP'",
        {"records": [], "totalSize": 0},
    )

    class Silent:
        def send(self, *a, **kw):
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())

    first = await poller.poll()
    assert first["created"] == 1

    # After tick 1: pretend SF now returns an existing Onboarding__c.
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_IDEMP'",
        {"records": [{"Id": "a01NEW"}], "totalSize": 1},
    )

    second = await poller.poll()
    assert second["skipped"] == 1
    assert second["created"] == 0


@pytest.mark.asyncio
async def test_poll_one_bad_opp_does_not_block_batch(
    make_opp, fake_sf_monkeypatch, monkeypatch
):
    fake_sf_monkeypatch.set_soql("Onboarding_Record_Created__c",
                                 {"records": [{"QualifiedApiName": "x"}]})
    good = make_opp(opp_id="006_GOOD")
    bad = make_opp(opp_id="006_BAD")
    fake_sf_monkeypatch.set_soql(
        "StageName = 'Closed Won'",
        {"records": [bad, good], "totalSize": 2, "done": True},
    )
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_GOOD'",
        {"records": [], "totalSize": 0},
    )
    fake_sf_monkeypatch.set_soql(
        "FROM Onboarding__c WHERE Opportunity__c = '006_BAD'",
        {"records": [], "totalSize": 0},
    )

    # Make create_record fail for 006_BAD, succeed for 006_GOOD.
    real_create = fake_sf_monkeypatch.create_record

    def wrapped(sobject, fields, *, agent_name, approval_gate_id=None):
        if fields.get("Opportunity__c") == "006_BAD":
            raise RuntimeError("SF rejected write")
        return real_create(sobject, fields,
                           agent_name=agent_name,
                           approval_gate_id=approval_gate_id)

    from shared.mcp import salesforce_mcp
    monkeypatch.setattr(salesforce_mcp, "create_record", wrapped)

    class Silent:
        def send(self, *a, **kw):
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())

    summary = await poller.poll()
    assert summary["created"] == 1
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["opp"] == "006_BAD"
