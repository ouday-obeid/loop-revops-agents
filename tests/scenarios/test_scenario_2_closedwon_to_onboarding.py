"""Scenario 2 — Closed-won Opportunity → Onboarding__c → CSM alert.

Monday item: 11736873545
Path: Sales Reps (closes the opp; not exercised here) → Onboarding
(closed_won_poller picks it up, creates Onboarding__c via self-approved gate,
posts CS alert) → CS (alert recipient; we only assert the Slack handoff shape).

Boundaries validated:

  1. Strategy-A dedup probe succeeds (Onboarding_Record_Created__c exists)
     so the poller uses the cheap SOQL.
  2. Belt-and-suspenders existence check returns empty → create_record fires.
  3. Gate was self-approved `single_record_update` tier (onboarding_auto_create
     origin marker in payload) and carried through to the SF create call.
  4. Slack notification to #cs-team was emitted with account name + onboarding
     record fields — this is the "CSM alert" boundary; the CS agent consumes
     this channel separately.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from agents.onboarding import closed_won_poller as poller
from shared.db.connection import get_engine


@pytest.mark.asyncio
async def test_closed_won_creates_onboarding_and_notifies_cs(
    sf_monkeypatch, slack_capture, make_opp
):
    fake = sf_monkeypatch

    # Strategy-A field probe → field exists.
    fake.set_soql(
        "Onboarding_Record_Created__c",
        {"records": [{"QualifiedApiName": "Onboarding_Record_Created__c"}]},
    )

    opp = make_opp(
        opp_id="006SCEN2_A", account_name="Acme Restaurants",
        owner_id="005OWNER00000000AAA",
    )
    # Closed-won SOQL returns our opp.
    fake.set_soql(
        "StageName = 'Closed Won'",
        {"records": [opp], "totalSize": 1, "done": True},
    )
    # Belt-and-suspenders per-opp probe → no existing Onboarding__c.
    fake.set_soql(
        f"FROM Onboarding__c WHERE Opportunity__c = '{opp['Id']}'",
        {"records": [], "totalSize": 0},
    )

    summary = await poller.poll()

    # Boundary 1 — Strategy A + one creation happened.
    assert summary["strategy"] == "A"
    assert summary["candidates"] == 1
    assert summary["created"] == 1
    assert summary["skipped"] == 0
    assert not summary["errors"]

    # Boundary 2 — exactly one Onboarding__c create_record with the right shape.
    onboardings = [c for c in fake.created if c["sobject"] == "Onboarding__c"]
    assert len(onboardings) == 1
    fields = onboardings[0]["fields"]
    assert fields["Opportunity__c"] == opp["Id"]
    assert fields["Overall_Onboarding_Status__c"] == "Not Started"
    assert fields["JK_Onboarding_Stage__c"] == "Getting Access"
    assert fields["OwnerId"] == opp["OwnerId"]
    # Required boolean scaffold present.
    assert fields["Balance_Included__c"] is False
    assert fields["Headroom_Analysis__c"] is False

    # Boundary 3 — gate carried through. We do NOT enforce the specific ID
    # (autoincrement is environment-sensitive); we assert the payload origin
    # and status on the gate referenced by the create_record call.
    gate_id = onboardings[0]["approval_gate_id"]
    assert gate_id is not None
    engine = get_engine()
    with engine.begin() as conn:
        gate = conn.execute(
            text(
                "SELECT action_type, status, approved_by, payload FROM approval_gates "
                "WHERE id = :id"
            ),
            {"id": gate_id},
        ).mappings().first()
    assert gate is not None
    assert gate["action_type"] == "single_record_update"
    assert gate["status"] == "approved"
    payload = json.loads(gate["payload"])
    assert payload["origin"] == "onboarding_auto_create"
    assert payload["opportunity_id"] == opp["Id"]

    # Boundary 4 — CS team notification fired. Channel defaults to #cs-team
    # per onboarding_record_creator._notify_cs_team.
    cs_notifications = [
        s for s in slack_capture.sent
        if s["channel"] == "#cs-team" and "Acme Restaurants" in (s["text"] or "")
    ]
    assert len(cs_notifications) == 1
    assert "New onboarding created" in cs_notifications[0]["text"]
    assert opp["Id"] in cs_notifications[0]["text"]
