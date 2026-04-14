"""Scenario 1 — Lead → qualified → demo booked.

Monday item: 11736875950
Path: Top of Funnel (enrichment + routing + lead create) → Sales Reps (pre-demo
GCal scan + opp resolution).

What we actually validate end-to-end:

  1. ToF lead writer builds a schema-aware Lead payload from an agent dict,
     assigns OwnerId from routing, and writes via an approved gate (one
     governance boundary).
  2. Sales Reps `pre_demo.trigger.resolve_opportunity` converts the lead's
     email on a gcal event into the Opportunity that the brief generator will
     key off of — the handoff point from ToF-created lead → Sales Reps demo
     prep. We don't go all the way to Fireflies+brief composition here; that
     is validated in the sales_reps per-agent suite.

Why focus here: the primary Phase 1 DoD question for this scenario is "does
a ToF-sourced lead survive into a Sales Reps demo brief context?" — the
payload → owner → SOQL-by-email round-trip. Full pipeline wiring
(Apollo→Clay→Slack briefing) lives in `agents/top_of_funnel/tests/test_end_to_end.py`.
"""
from __future__ import annotations

from typing import Any

import pytest

from agents.sales_reps.pre_demo import trigger
from agents.top_of_funnel import sf_lead_writer
from shared.governance import create_approval_gate, decide_approval_gate


@pytest.mark.asyncio
async def test_tof_lead_create_hands_off_to_sales_reps_demo_resolver(sf_monkeypatch):
    """End-to-end: ToF writes a Lead with OwnerId=Carlton (MM), then Sales Reps
    resolves the same external email back to an Opportunity when the demo
    lands on GCal.
    """
    fake = sf_monkeypatch

    # --- ToF half ---------------------------------------------------------

    # Describe: every custom field present so the payload uses real SF fields
    # rather than the Description fallback (we want to assert ICP_Score__c).
    fake.set_describe(
        "Lead",
        {
            "fields": [
                {"name": n}
                for n in (
                    "ICP_Score__c",
                    "ICP_Tier__c",
                    "Brand__c",
                    "Ownership_Type__c",
                    "Location_Count__c",
                )
            ]
        },
    )
    # No existing Lead/Contact/Account → dedup probe passes.
    fake.set_soql("FROM Lead WHERE Email", {"records": []})
    fake.set_soql("FROM Contact WHERE Email", {"records": []})
    fake.set_soql("FROM Account WHERE Website", {"records": []})
    # No TLO match → tlo_id=None; payload still builds.
    fake.set_soql("FROM Top_Level_Org__c WHERE", {"records": []})

    gate_id = create_approval_gate(
        agent_name="top_of_funnel",
        action_type="single_record_update",
        payload={"origin": "scenario_1_lead_create"},
        justification=None,
    )
    decide_approval_gate(gate_id, approved=True, approver="system:scenario")

    lead = {
        "first_name": "Alex",
        "last_name": "Hughes",
        "email": "alex@acmerestaurants.com",
        "company_name": "Acme Restaurants",
        "domain": "acmerestaurants.com",
        "icp_score": 82,
        "icp_tier": "A",
        "brand": "Arby's",
        "ownership_type": "franchise_group",
        "location_count": 47,
        "assigned_sdr_id": "005CARLTON00000000",  # from routing.assign_owner
    }

    result = sf_lead_writer.create_lead(lead, approval_gate_id=gate_id)

    # Boundary 1 — Lead was actually created via an approved gate, owner set.
    assert result["sf_id"] is not None
    assert not result["skipped"]
    assert len(fake.created) == 1
    created = fake.created[0]
    assert created["sobject"] == "Lead"
    assert created["fields"]["OwnerId"] == "005CARLTON00000000"
    assert created["fields"]["Email"] == "alex@acmerestaurants.com"
    # Custom fields present — no Description fallback.
    assert created["fields"]["ICP_Score__c"] == 82
    assert created["fields"]["ICP_Tier__c"] == "A"
    assert created["approval_gate_id"] == gate_id

    # --- Sales Reps half --------------------------------------------------

    # The SDR books a demo; GCal event lands with the same external attendee.
    # resolve_opportunity SOQLs OpportunityContactRole by Contact.Email and
    # should return the opp. We register a matching row.
    fake.set_soql(
        "FROM OpportunityContactRole WHERE Contact.Email IN",
        {
            "records": [
                {
                    "OpportunityId": "006DEMO00000000001",
                    "Opportunity": {
                        "Name": "Acme — New Business 2026",
                        "StageName": "Qualified",
                        "Amount": 24000,
                        "CloseDate": "2026-07-01",
                        "IsClosed": False,
                        "AccountId": "001ACME000000000AAA",
                        "Account": {
                            "Name": "Acme Restaurants",
                            "Website": "https://acmerestaurants.com",
                        },
                        "Owner": {"Email": "carlton@tryloop.ai"},
                    },
                }
            ]
        },
    )

    gcal_event = {
        "title": "Loop AI x Acme — Product Demo",
        "attendees": [
            {"email": "carlton@tryloop.ai"},  # internal, filtered out
            {"email": "alex@acmerestaurants.com"},  # external, keyed for resolve
        ],
    }

    assert trigger.is_demo(gcal_event) is True
    externals = trigger.external_attendees(gcal_event)
    assert externals == ["alex@acmerestaurants.com"]

    opp = trigger.resolve_opportunity(externals)

    # Boundary 2 — the lead-owning AE's opp is the one that will drive the
    # pre-demo brief. Name + StageName + OwnerEmail round-tripped.
    assert opp is not None
    assert opp["Id"] == "006DEMO00000000001"
    assert opp["StageName"] == "Qualified"
    assert opp["AccountName"] == "Acme Restaurants"
    assert opp["OwnerEmail"] == "carlton@tryloop.ai"


def test_sales_reps_filters_internal_only_events(sf_monkeypatch):
    """Regression: an internal-only calendar event must not trigger a brief."""
    event = {
        "title": "AE Sync — Weekly",
        "attendees": [
            {"email": "carlton@tryloop.ai"},
            {"email": "hutch@tryloop.ai"},
        ],
    }
    assert trigger.is_demo(event) is False
    assert trigger.external_attendees(event) == []
