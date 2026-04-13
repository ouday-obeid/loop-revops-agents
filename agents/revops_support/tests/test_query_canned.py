"""Canned query tests — mock salesforce_mcp.soql_query to return fake records,
verify each canned query normalizes rows and packages text + blocks.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.revops_support.query import canned, soql_engine


@pytest.fixture
def fake_sf():
    with patch("shared.mcp.salesforce_mcp.soql_query") as m:
        yield m


def test_pipeline_by_stage_normalizes_aggregates(fake_sf):
    fake_sf.return_value = {
        "records": [
            {"StageName": "Qualification", "expr0": 12, "expr1": 240000},
            {"StageName": "Proposal", "expr0": 5, "expr1": 500000},
        ]
    }
    result = canned.pipeline_by_stage()
    assert len(result["records"]) == 2
    assert result["records"][0]["stage"] == "Qualification"
    assert result["records"][0]["count"] == 12
    assert "Pipeline by Stage" in result["text"]
    assert result["blocks"][0]["type"] == "section"


def test_stale_opportunities_uses_days(fake_sf):
    fake_sf.return_value = {
        "records": [
            {
                "Id": "006X",
                "Name": "Big deal",
                "StageName": "Proposal",
                "Amount": 100000,
                "LastModifiedDate": "2026-02-01T00:00:00Z",
                "Owner": {"Name": "Alex"},
            }
        ]
    }
    result = canned.stale_opportunities(days=45)
    assert result["records"][0]["owner"] == "Alex"
    query_run = fake_sf.call_args[0][0]
    assert "LAST_N_DAYS:45" in query_run


def test_duplicate_contacts_by_email(fake_sf):
    fake_sf.return_value = {
        "records": [
            {"Email": "a@x.com", "expr0": 3},
            {"Email": "b@y.com", "expr0": 2},
        ]
    }
    result = canned.duplicate_contacts_by_email()
    assert result["records"][0]["count"] == 3
    assert result["records"][0]["email"] == "a@x.com"


def test_empty_result_renders_no_results(fake_sf):
    fake_sf.return_value = {"records": []}
    result = canned.tlos_with_no_opps()
    assert "no results" in result["text"]


def test_soql_engine_rejects_dml():
    with pytest.raises(soql_engine.SOQLError):
        soql_engine.run("DELETE FROM Account WHERE Id = '001'")


def test_soql_engine_rejects_non_select():
    with pytest.raises(soql_engine.SOQLError):
        soql_engine.run("UPDATE Account SET Name = 'x'")


def test_soql_engine_adds_limit_when_missing(fake_sf):
    fake_sf.return_value = {"records": []}
    soql_engine.run("SELECT Id FROM Account")
    assert "LIMIT 100" in fake_sf.call_args[0][0]


def test_soql_engine_respects_existing_limit(fake_sf):
    fake_sf.return_value = {"records": []}
    soql_engine.run("SELECT Id FROM Account LIMIT 5")
    q = fake_sf.call_args[0][0]
    assert "LIMIT 5" in q
    # LIMIT should appear only once
    assert q.upper().count("LIMIT") == 1


def test_validation_rule_violations_rejects_invalid_object():
    with pytest.raises(soql_engine.SOQLError):
        canned.validation_rule_violations("Account; DROP TABLE")


def test_registry_has_eight_queries():
    assert set(canned.REGISTRY.keys()) == {
        "pipeline_by_stage",
        "stale_opportunities",
        "tlos_with_no_opps",
        "opps_missing_products",
        "accounts_with_no_tlo",
        "duplicate_contacts_by_email",
        "active_users_with_login",
        "validation_rule_violations",
    }
