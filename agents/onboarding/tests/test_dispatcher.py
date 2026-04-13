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
             "Account__r": {"Name": "Acme"}},
            {"Id": "a01DEF", "Name": "Beta Onboarding",
             "Account__r": {"Name": "Beta"}},
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
