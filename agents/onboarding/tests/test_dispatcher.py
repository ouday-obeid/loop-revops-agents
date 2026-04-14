"""Dispatcher routing tests — ping, help, argument validation, registry wiring."""
from __future__ import annotations

import pytest

from agents.onboarding.dispatcher import HELP_TEXT, OnboardingDispatcher


@pytest.mark.asyncio
async def test_ping_returns_pong(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="ping"))
    assert "pong" in result["text"].lower()
    assert "onboarding online" in result["text"].lower()


@pytest.mark.asyncio
async def test_empty_text_returns_pong(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text=""))
    assert "pong" in result["text"].lower()


@pytest.mark.asyncio
async def test_help_returns_command_list(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="help"))
    assert result["text"] == HELP_TEXT


@pytest.mark.asyncio
async def test_unknown_command_shows_help(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="frobnicate"))
    assert "unknown onboarding command" in result["text"].lower()
    assert "frobnicate" in result["text"]


@pytest.mark.asyncio
async def test_status_requires_account(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="status"))
    assert "usage" in result["text"].lower()
    assert "<account>" in result["text"]


@pytest.mark.asyncio
async def test_handoff_requires_account(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="handoff"))
    assert "usage" in result["text"].lower()


@pytest.mark.asyncio
async def test_stalls_accepts_days_argument(ob_payload, monkeypatch):
    from agents.onboarding import milestone_monitor

    async def _fake_find_stalls(min_business_days: int = 5):
        assert min_business_days == 7
        return []

    monkeypatch.setattr(milestone_monitor, "find_stalls", _fake_find_stalls)
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="stalls 7"))
    assert "no onboardings stalled" in result["text"].lower()
    assert "7 business days" in result["text"]


@pytest.mark.asyncio
async def test_stalls_rejects_non_integer(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="stalls banana"))
    assert "usage" in result["text"].lower()
    assert "banana" in result["text"]


@pytest.mark.asyncio
async def test_backfill_requires_preview_flag(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="backfill"))
    assert "--preview" in result["text"]
    assert "writes are disabled" in result["text"].lower()


@pytest.mark.asyncio
async def test_backfill_preview_reports_count(ob_payload, fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 42, "done": True})
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="backfill --preview")
    )
    assert "*42*" in result["text"]
    assert "No writes performed" in result["text"]


@pytest.mark.asyncio
async def test_unassigned_empty_returns_clean_message(ob_payload, fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 0, "done": True})
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="unassigned"))
    assert "no unassigned onboardings" in result["text"].lower()


@pytest.mark.asyncio
async def test_unassigned_lists_rows(ob_payload, fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "a01ABC", "Name": "Acme Onboarding",
             "Opportunity__r": {"Account": {"Name": "Acme"}}},
            {"Id": "a01DEF", "Name": "Beta Onboarding",
             "Opportunity__r": {"Account": {"Name": "Beta"}}},
        ],
        "totalSize": 2, "done": True,
    })
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="unassigned"))
    assert "Acme" in result["text"]
    assert "Beta" in result["text"]


@pytest.mark.asyncio
async def test_status_no_account_match(ob_payload, fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 0, "done": True})
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="status NonExistent Cafe")
    )
    assert "no account matched" in result["text"].lower()


@pytest.mark.asyncio
async def test_status_account_but_no_onboarding(ob_payload, fake_sf_monkeypatch):
    fake = fake_sf_monkeypatch
    # First SOQL = account match; second SOQL = no onboarding records
    fake.queue_soql({
        "records": [{"Id": "001ABC", "Name": "Acme Restaurants"}],
        "totalSize": 1, "done": True,
    })
    fake.queue_soql({"records": [], "totalSize": 0, "done": True})
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="status Acme")
    )
    assert "Acme Restaurants" in result["text"]
    assert "no `Onboarding__c` record" in result["text"]


@pytest.mark.asyncio
async def test_status_renders_onboarding_snapshot(ob_payload, fake_sf_monkeypatch):
    fake = fake_sf_monkeypatch
    fake.queue_soql({
        "records": [{"Id": "001ABC", "Name": "Acme"}],
        "totalSize": 1, "done": True,
    })
    fake.queue_soql({
        "records": [{
            "Id": "a01ABC",
            "Name": "Acme Onboarding",
            "JK_Onboarding_Stage__c": "Initial Onboarding Scheduled",
            "Overall_Onboarding_Status__c": "In Progress",
            "Kickoff_Status__c": "Kickoff Scheduled",
            "Onboarding_Health__c": "Healthy",
            "OwnerId": "005OWNER",
            "CSM_2__c": "005CSM2",
            "LastModifiedDate": "2026-04-12T10:00:00Z",
        }],
        "totalSize": 1, "done": True,
    })
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="status Acme")
    )
    assert "Initial Onboarding Scheduled" in result["text"]
    assert "005OWNER" in result["text"]
    assert "CSM 2" in result["text"]


@pytest.mark.asyncio
async def test_skip_requires_justification(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="skip 006ABC"))
    assert "Usage:" in result["text"]
    assert "Justification is required" in result["text"]


@pytest.mark.asyncio
async def test_skip_rejects_bad_opp_id(ob_payload):
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="skip 001NOTANOPP reason here")
    )
    assert "doesn't look like an Opportunity Id" in result["text"]


@pytest.mark.asyncio
async def test_skip_creates_gate_with_justification(ob_payload, fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "a01WB000001234567"}], "totalSize": 1, "done": True,
    })
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(
            text="skip 006WB00000JcUVDYA3 trial contract, no line items by design",
            user="U_JACKIE",
        )
    )
    assert "skip_milestone gate" in result["text"]
    assert "006WB00000JcUVDYA3" in result["text"]
    assert "trial contract" in result["text"]

    from sqlalchemy import text as sql_text
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text("SELECT action_type, status, justification, payload, "
                     "requested_by FROM approval_gates "
                     "WHERE agent_name='onboarding' "
                     "ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert row[0] == "skip_milestone"
    assert row[1] == "pending"
    assert "trial contract" in row[2]
    assert "006WB00000JcUVDYA3" in row[3]
    assert row[4] == "U_JACKIE"


@pytest.mark.asyncio
async def test_assign_usage_when_missing_args(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="assign"))
    assert "Usage:" in result["text"]
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="assign 42"))
    assert "Usage:" in result["text"]


@pytest.mark.asyncio
async def test_assign_rejects_non_integer_gate(ob_payload):
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="assign abc 005OWN000000001")
    )
    assert "not a valid gate id" in result["text"]


@pytest.mark.asyncio
async def test_assign_rejects_bad_user_id(ob_payload):
    result = await OnboardingDispatcher().handle(
        "slack", ob_payload(text="assign 42 001NOTAUSER")
    )
    assert "doesn't look like a Salesforce User Id" in result["text"]


@pytest.mark.asyncio
async def test_assign_complains_when_gate_not_approved(
    ob_payload, fake_sf_monkeypatch, seed_gate,
):
    gate_id = seed_gate(
        action_type="csm_reassignment",
        status="pending",
        payload={"onboarding_id": "a01WB0000001ABCAAA"},
    )
    result = await OnboardingDispatcher().handle(
        "slack",
        ob_payload(text=f"assign {gate_id} 005OWN000000001", user="U_JACKIE"),
    )
    assert "is not ready" in result["text"]


@pytest.mark.asyncio
async def test_assign_applies_reassignment_on_approved_gate(
    ob_payload, fake_sf_monkeypatch, seed_gate,
):
    gate_id = seed_gate(
        action_type="csm_reassignment",
        status="approved",
        payload={"onboarding_id": "a01WB0000001ABCAAA"},
    )
    result = await OnboardingDispatcher().handle(
        "slack",
        ob_payload(text=f"assign {gate_id} 005NEW000000001", user="U_JACKIE"),
    )
    assert "reassigned" in result["text"].lower()
    assert "005NEW000000001" in result["text"]
    assert "a01WB0000001ABCAAA" in result["text"]

    updated = fake_sf_monkeypatch.updated
    assert len(updated) == 1
    assert updated[0]["sobject"] == "Onboarding__c"
    assert updated[0]["id"] == "a01WB0000001ABCAAA"
    assert updated[0]["fields"]["OwnerId"] == "005NEW000000001"


@pytest.mark.asyncio
async def test_help_mentions_new_commands(ob_payload):
    result = await OnboardingDispatcher().handle("slack", ob_payload(text="help"))
    assert "skip" in result["text"]
    assert "assign" in result["text"]


@pytest.mark.asyncio
async def test_registration_exposes_handler():
    """bootstrap() must register the onboarding handler on the shared dispatcher."""
    from agents.onboarding import main as ob_main
    from shared import slack_dispatcher

    ob_main.bootstrap()
    agent, rest = slack_dispatcher.parse_command("onboarding ping")
    assert agent == "onboarding"
    assert rest == "ping"

    result = await slack_dispatcher.dispatch(
        "onboarding ping", {"user": "U_TEST", "channel": "C_TEST"}
    )
    assert "pong" in result["text"].lower()
