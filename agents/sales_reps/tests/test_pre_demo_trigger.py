"""Pre-demo trigger — title filtering, external-attendee extraction, Opp resolve."""
from __future__ import annotations

from unittest.mock import patch

from agents.sales_reps.pre_demo import trigger


def _event(**kw):
    base = {
        "id": "EVT_1", "title": "Loop Demo — Acme", "start": "2026-04-13T16:00:00Z",
        "attendees": [
            {"email": "ae@tryloop.ai"},
            {"email": "buyer@acme.com"},
        ],
    }
    base.update(kw)
    return base


def test_is_demo_requires_title_match():
    assert trigger.is_demo(_event(title="Loop Demo — Acme")) is True
    assert trigger.is_demo(_event(title="Internal Standup")) is False


def test_is_demo_requires_external_attendee():
    # Even a "Demo" title is not a demo if only internal folks are invited.
    evt = _event(title="Demo rehearsal", attendees=[{"email": "a@tryloop.ai"}])
    assert trigger.is_demo(evt) is False


def test_is_demo_intro_and_discovery_match():
    assert trigger.is_demo(_event(title="Intro with Acme")) is True
    assert trigger.is_demo(_event(title="Discovery — Acme")) is True
    assert trigger.is_demo(_event(title="Call with buyer")) is True


def test_external_attendees_drops_tryloop():
    evt = _event(attendees=[
        {"email": "ae@tryloop.ai"},
        {"email": "buyer@acme.com"},
        {"email": "cto@acme.com"},
    ])
    out = trigger.external_attendees(evt)
    assert sorted(out) == ["buyer@acme.com", "cto@acme.com"]


def test_external_attendees_empty_when_all_internal():
    evt = _event(attendees=[{"email": "a@tryloop.ai"}, {"email": "b@tryloop.ai"}])
    assert trigger.external_attendees(evt) == []


def test_resolve_opportunity_returns_first_match():
    fake = {"records": [{
        "OpportunityId": "006X",
        "Opportunity": {
            "Name": "Acme - Loop", "StageName": "Proposal", "Amount": 25000,
            "CloseDate": "2026-05-01", "AccountId": "001A",
            "Account": {"Name": "Acme Corp", "Website": "https://acme.com"},
            "Owner": {"Email": "ae@tryloop.ai"},
        },
    }]}
    with patch.object(trigger.salesforce_mcp, "soql_query", return_value=fake):
        out = trigger.resolve_opportunity(["buyer@acme.com"])
    assert out["Id"] == "006X"
    assert out["AccountName"] == "Acme Corp"
    assert out["Website"] == "https://acme.com"


def test_resolve_opportunity_none_for_empty_emails():
    assert trigger.resolve_opportunity([]) is None


def test_resolve_opportunity_degrades_on_soql_error():
    with patch.object(trigger.salesforce_mcp, "soql_query",
                      side_effect=RuntimeError("SF down")):
        out = trigger.resolve_opportunity(["a@b.com"])
    assert out is None


def test_scan_upcoming_filters_to_demo_like_events():
    fake_events = [
        _event(id="E1", title="Loop Demo — Acme"),
        _event(id="E2", title="Internal RevOps Sync",
               attendees=[{"email": "a@tryloop.ai"}]),
        _event(id="E3", title="Call with buyer"),
    ]
    fake_opp = {"Id": "006Z", "AccountName": "Acme"}
    with patch.object(trigger.gcal, "list_upcoming", return_value=fake_events), \
         patch.object(trigger, "resolve_opportunity", return_value=fake_opp):
        out = trigger.scan_upcoming()
    ids = [entry["event"]["id"] for entry in out]
    assert ids == ["E1", "E3"]


def test_scan_upcoming_degrades_on_gcal_error():
    with patch.object(trigger.gcal, "list_upcoming",
                      side_effect=RuntimeError("gcal 500")):
        out = trigger.scan_upcoming()
    assert out and "error" in out[0]
