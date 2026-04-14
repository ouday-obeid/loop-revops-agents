"""Fetcher tests — SOQL shape, OppRecord parsing, ICP fallback.

SF MCP is monkeypatched at the module-level `salesforce_mcp.soql_query` so the
fetcher's import path is exercised exactly as it runs in prod. We capture the
query string and can assert field presence without string-parsing the SQL.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from agents.slt_metrics.pipeline import fetcher
from agents.slt_metrics.pipeline.config import PRODUCT_FIELDS
from agents.slt_metrics.types import OppRecord
from shared.mcp import salesforce_mcp


# ------------------------------------------------------------------ SOQL builder

def test_soql_includes_core_opp_fields():
    soql = fetcher.build_open_pipeline_soql()
    for field_ in ("Id", "Name", "StageName", "Amount", "ACV__c", "CloseDate",
                   "Segment__c", "LastActivityDate", "Time_in_Stage__c"):
        assert field_ in soql


def test_soql_includes_account_and_owner_relations():
    soql = fetcher.build_open_pipeline_soql()
    for rel in ("Account.Name", "Account.Website", "Account.Type",
                "Owner.Name", "Owner.UserRole.Name", "Owner.Manager.Name"):
        assert rel in soql


def test_soql_includes_every_product_field():
    soql = fetcher.build_open_pipeline_soql()
    for sf_field in PRODUCT_FIELDS:
        assert sf_field in soql, f"missing product field {sf_field}"


def test_soql_includes_contact_roles_subquery():
    soql = fetcher.build_open_pipeline_soql()
    assert "OpportunityContactRoles" in soql
    assert "Contact.Email" in soql


def test_soql_has_explicit_limit_and_order():
    soql = fetcher.build_open_pipeline_soql(limit=1000)
    # Explicit LIMIT 1000 (salesforce_mcp auto-injects LIMIT 100 if missing).
    assert "LIMIT 1000" in soql
    assert "ORDER BY ACV__c DESC NULLS LAST" in soql
    assert "IsClosed = false" in soql


def test_soql_include_icp_false_drops_icp_column():
    with_icp = fetcher.build_open_pipeline_soql(include_icp=True)
    without_icp = fetcher.build_open_pipeline_soql(include_icp=False)
    assert "ICP_Score__c" in with_icp
    assert "ICP_Score__c" not in without_icp


def test_soql_honors_date_window_overrides():
    soql = fetcher.build_open_pipeline_soql(
        date_from="LAST_N_DAYS:30", date_to="NEXT_N_DAYS:90"
    )
    assert "CloseDate >= LAST_N_DAYS:30" in soql
    assert "CloseDate <= NEXT_N_DAYS:90" in soql


# ------------------------------------------------------------------ parsing

def _sf_row(**overrides: Any) -> dict[str, Any]:
    base = {
        "Id": "0061x00000ABC001",
        "Name": "Chick-fil-A — MM Q2",
        "AccountId": "0011x00000AAA001",
        "Account": {
            "Name": "Chick-fil-A Franchise Group",
            "Website": "cfa.com",
            "Type": "Customer - Direct",
        },
        "OwnerId": "0051x00000OWN001",
        "Owner": {
            "Name": "Nate Renner",
            "UserRole": {"Name": "MM Sales"},
            "Manager": {"Name": "Hutch"},
        },
        "StageName": "Proposal",
        "IsClosed": False,
        "IsWon": False,
        "Amount": 120000.0,
        "ACV__c": 90000.0,
        "Fixed_ARR__c": 90000.0,
        "Locations__c": 42,
        "Type": "New Business",
        "LeadSource": "Inbound",
        "CloseDate": "2026-06-30",
        "CreatedDate": "2026-03-01T14:22:00.000+0000",
        "LastActivityDate": "2026-04-10",
        "LastModifiedDate": "2026-04-12T09:15:00.000+0000",
        "LastStageChangeDate": "2026-04-05",
        "D_T_Last_Stage_Change__c": 8,
        "Time_in_Stage__c": 12,
        "Probability": 75.0,
        "Description": None,
        "Next_Steps__c": "Redline review",
        "Next_Step_Date__c": "2026-04-20",
        "ICP_Score__c": 0.87,
        "Segment__c": "MM",
        "Count_Balance__c": 1,
        "Count_TruROI__c": 2,
        "Count_White_Glove__c": 0,
        "OpportunityContactRoles": {
            "records": [
                {
                    "ContactId": "0031x00000CON001",
                    "Contact": {
                        "Name": "Jane Buyer",
                        "Email": "jane@cfa.com",
                        "Title": "VP Ops",
                    },
                    "Role": "Economic Buyer",
                    "IsPrimary": True,
                }
            ]
        },
    }
    base.update(overrides)
    return base


def test_parse_record_roundtrip_core_fields():
    opp = fetcher._parse_record(_sf_row())
    assert isinstance(opp, OppRecord)
    assert opp.id == "0061x00000ABC001"
    assert opp.account_name == "Chick-fil-A Franchise Group"
    assert opp.owner_name == "Nate Renner"
    assert opp.owner_role == "MM Sales"
    assert opp.owner_manager == "Hutch"
    assert opp.stage == "Proposal"
    assert opp.acv == 90000.0
    assert opp.segment == "MM"
    assert opp.icp_score == 0.87
    assert opp.locations == 42
    assert opp.close_date == date(2026, 6, 30)
    assert isinstance(opp.created_date, datetime)


def test_parse_record_products_mapped_to_canonical_names():
    opp = fetcher._parse_record(_sf_row())
    # Zero-count fields still appear so callers can distinguish "no product"
    # from "field absent" — the fetcher keeps zeros explicitly.
    assert opp.products["Balance"] == 1
    assert opp.products["TruROI"] == 2
    assert opp.products["White Glove"] == 0
    # Fields not set on the row should NOT appear.
    assert "Guard" not in opp.products


def test_parse_record_contact_roles():
    opp = fetcher._parse_record(_sf_row())
    assert len(opp.contact_roles) == 1
    cr = opp.contact_roles[0]
    assert cr.contact_id == "0031x00000CON001"
    assert cr.email == "jane@cfa.com"
    assert cr.title == "VP Ops"
    assert cr.is_primary is True


def test_parse_record_handles_missing_nested_relations():
    row = _sf_row(Account=None, Owner=None, OpportunityContactRoles=None)
    opp = fetcher._parse_record(row)
    assert opp.account_name is None
    assert opp.owner_name is None
    assert opp.owner_role is None
    assert opp.owner_manager is None
    assert opp.contact_roles == []


def test_parse_record_coerces_dirty_numbers():
    row = _sf_row(Amount="", ACV__c="90000", Locations__c="42.0", Probability=None)
    opp = fetcher._parse_record(row)
    assert opp.amount is None
    assert opp.acv == 90000.0
    assert opp.locations == 42
    assert opp.probability_sf is None


# ------------------------------------------------------------------ fetch_open_opps

def test_fetch_open_opps_calls_soql_with_built_query(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_soql(query: str, limit: int = 100):
        captured["query"] = query
        captured["limit"] = limit
        return {"records": [_sf_row()]}

    monkeypatch.setattr(salesforce_mcp, "soql_query", fake_soql)
    opps = fetcher.fetch_open_opps()
    assert len(opps) == 1
    assert opps[0].id == "0061x00000ABC001"
    assert "IsClosed = false" in captured["query"]
    assert captured["limit"] == 1000


def test_fetch_open_opps_retries_without_icp_on_invalid_field(monkeypatch):
    calls: list[str] = []

    def fake_soql(query: str, limit: int = 100):
        calls.append(query)
        if "ICP_Score__c" in query:
            raise salesforce_mcp.SalesforceError(
                "INVALID_FIELD: No such column 'ICP_Score__c' on entity 'Opportunity'"
            )
        return {"records": [_sf_row(ICP_Score__c=None)]}

    monkeypatch.setattr(salesforce_mcp, "soql_query", fake_soql)
    opps = fetcher.fetch_open_opps()
    assert len(calls) == 2
    assert "ICP_Score__c" in calls[0]
    assert "ICP_Score__c" not in calls[1]
    assert opps[0].icp_score is None


def test_fetch_open_opps_propagates_non_icp_errors(monkeypatch):
    def fake_soql(query: str, limit: int = 100):
        raise salesforce_mcp.SalesforceError("MALFORMED_QUERY: unexpected token")

    monkeypatch.setattr(salesforce_mcp, "soql_query", fake_soql)
    with pytest.raises(salesforce_mcp.SalesforceError):
        fetcher.fetch_open_opps()


def test_fetch_open_opps_empty_result(monkeypatch):
    monkeypatch.setattr(
        salesforce_mcp, "soql_query", lambda q, limit=100: {"records": []}
    )
    assert fetcher.fetch_open_opps() == []
