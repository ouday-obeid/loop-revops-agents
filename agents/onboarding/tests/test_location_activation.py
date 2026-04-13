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


def test_resolved_field_returns_exact_match(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c", {"fields": [{"name": "Activation_Status__c"}]}
    )
    assert loc._resolved_field("Activation_Status__c") == "Activation_Status__c"


def test_resolved_field_returns_fuzzy_fallback(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c", {"fields": [{"name": "Activation_State__c"}]}
    )
    resolved = loc._resolved_field("Activation_Status__c")
    assert resolved == "Activation_State__c"


def test_resolved_field_returns_none_when_no_match(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe("Location__c",
                                     {"fields": [{"name": "Other__c"}]})
    assert loc._resolved_field("Activation_Status__c") is None


def test_resolved_field_uses_candidate_on_describe_exception(monkeypatch):
    from shared.mcp import salesforce_mcp

    def boom(*a, **kw):
        raise RuntimeError("SF down")
    monkeypatch.setattr(salesforce_mcp, "describe_sobject", boom)
    assert loc._resolved_field("Activation_Status__c") == "Activation_Status__c"


@pytest.mark.asyncio
async def test_sweep_skips_when_schema_gap(fake_sf_monkeypatch):
    # describe returns no matching field → fuzzy fallback is None
    fake_sf_monkeypatch.set_describe("Location__c", {"fields": []})
    result = await loc.sweep()
    assert result.get("skipped") is True
    assert result["reason"] == "schema_gap"


@pytest.mark.asyncio
async def test_sweep_aggregates_by_reason(fake_sf_monkeypatch):
    fake_sf_monkeypatch.set_describe(
        "Location__c", {"fields": [{"name": "Activation_Status__c"}]}
    )
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "L1", "Account__c": "A1", "Account__r": {"Name": "Acme"},
             "Stuck_Reason__c": "Bank access",
             "Activation_Status__c": "Pending"},
            {"Id": "L2", "Account__c": "A1", "Account__r": {"Name": "Acme"},
             "Stuck_Reason__c": "Bank access",
             "Activation_Status__c": "Pending"},
            {"Id": "L3", "Account__c": "A2", "Account__r": {"Name": "Beta"},
             "Stuck_Reason__c": "POS integration",
             "Activation_Status__c": "Pending"},
        ],
        "totalSize": 3, "done": True,
    })
    summary = await loc.sweep()
    assert summary["total_stuck"] == 3
    assert summary["by_reason"]["Bank access"] == 2
    assert summary["by_reason"]["POS integration"] == 1
    assert summary["accounts_with_stuck"] == 2


@pytest.mark.asyncio
async def test_report_filters_by_account(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "L1", "Name": "Acme Store 1", "Account__r": {"Name": "Acme"},
             "Stuck_Reason__c": "Bank access",
             "Activation_Status__c": "Pending"},
            {"Id": "L2", "Name": "Beta Store 1", "Account__r": {"Name": "Beta"},
             "Stuck_Reason__c": "POS", "Activation_Status__c": "Pending"},
        ],
        "totalSize": 2, "done": True,
    })
    out = await loc.report(account_filter="Acme")
    assert "Acme" in out
    assert "Beta" not in out


@pytest.mark.asyncio
async def test_report_no_results_message(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 0, "done": True})
    out = await loc.report()
    assert "No stuck locations" in out
