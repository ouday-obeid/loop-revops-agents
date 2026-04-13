"""Location activation — schema resilience, classifier, report formatting."""
from __future__ import annotations

import pytest

from agents.onboarding import location_activation as loc


@pytest.fixture(autouse=True)
def _reset_field_cache():
    loc._FIELD_CACHE.clear()
    yield
    loc._FIELD_CACHE.clear()


def test_classify_counts_by_reason():
    rows = [
        {"Stuck_Reason__c": "Bank access"},
        {"Stuck_Reason__c": "Bank access"},
        {"Stuck_Reason__c": "POS integration"},
        {"Stuck_Reason__c": None},
    ]
    counts = loc.classify(rows)
    assert counts["Bank access"] == 2
    assert counts["POS integration"] == 1
    assert counts["(no reason set)"] == 1


def test_field_exists_true_when_present(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c", {"fields": [{"name": "Activation_Status__c"}]}
    )
    assert loc._field_exists("Activation_Status__c") is True


def test_field_exists_false_when_absent(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c", {"fields": [{"name": "Other__c"}]}
    )
    assert loc._field_exists("Activation_Status__c") is False


def test_field_exists_false_on_describe_exception(monkeypatch):
    from shared.mcp import salesforce_mcp

    def boom(*a, **kw):
        raise RuntimeError("SF down")
    monkeypatch.setattr(salesforce_mcp, "describe_sobject", boom)
    assert loc._field_exists("Activation_Status__c") is False


@pytest.mark.asyncio
async def test_sweep_skips_and_seeds_task_when_schema_gap(fake_sf_monkeypatch):
    # describe returns no matching field → schema_gap + Agent 5 task seeded
    fake_sf_monkeypatch.set_describe("Location__c", {"fields": []})
    result = await loc.sweep()
    assert result.get("skipped") is True
    assert result["reason"] == "schema_gap"
    assert set(result["missing"]) == set(loc.REQUIRED_FIELDS)

    # Verify the Agent 5 task was recorded
    from sqlalchemy import text as sql_text
    from shared.db.connection import get_engine
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT agent_name, title FROM tasks "
                "WHERE source = 'onboarding:location_schema_gap'"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "revops_support"
    assert "Location__c" in row[1]


@pytest.mark.asyncio
async def test_sweep_aggregates_by_reason(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c",
        {
            "fields": [
                {"name": "Activation_Status__c"},
                {"name": "Stuck_Reason__c"},
            ]
        },
    )
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "L1", "TLO__c": "T1", "TLO__r": {"Name": "Acme Group"},
             "Stuck_Reason__c": "Bank access",
             "Activation_Status__c": "Pending"},
            {"Id": "L2", "TLO__c": "T1", "TLO__r": {"Name": "Acme Group"},
             "Stuck_Reason__c": "Bank access",
             "Activation_Status__c": "Pending"},
            {"Id": "L3", "TLO__c": "T2", "TLO__r": {"Name": "Beta Group"},
             "Stuck_Reason__c": "POS integration",
             "Activation_Status__c": "Pending"},
        ],
        "totalSize": 3, "done": True,
    })
    summary = await loc.sweep()
    assert summary["total_inactive"] == 3
    assert summary["by_reason"]["Bank access"] == 2
    assert summary["by_reason"]["POS integration"] == 1
    assert summary["tlos_with_inactive"] == 2


@pytest.mark.asyncio
async def test_report_filters_by_tlo_name(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c",
        {
            "fields": [
                {"name": "Activation_Status__c"},
                {"name": "Stuck_Reason__c"},
            ]
        },
    )
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "L1", "Name": "Acme Store 1", "TLO__r": {"Name": "Acme"},
             "Stuck_Reason__c": "Bank access",
             "Activation_Status__c": "Pending"},
            {"Id": "L2", "Name": "Beta Store 1", "TLO__r": {"Name": "Beta"},
             "Stuck_Reason__c": "POS", "Activation_Status__c": "Pending"},
        ],
        "totalSize": 2, "done": True,
    })
    out = await loc.report(account_filter="Acme")
    assert "Acme" in out
    assert "Beta" not in out


@pytest.mark.asyncio
async def test_report_warns_when_schema_gap(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe("Location__c", {"fields": []})
    out = await loc.report()
    assert "unavailable" in out
    assert "Agent 5" in out


@pytest.mark.asyncio
async def test_report_no_results_message(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c",
        {
            "fields": [
                {"name": "Activation_Status__c"},
                {"name": "Stuck_Reason__c"},
            ]
        },
    )
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 0, "done": True})
    out = await loc.report()
    assert "No stuck locations" in out
