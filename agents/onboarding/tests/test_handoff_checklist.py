"""Handoff checklist — six seed checks + summarize + format."""
from __future__ import annotations

import os

import pytest

from agents.onboarding import handoff_checklist as h


# ---------- products_priced ----------

def test_products_priced_fails_with_zero_rows(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 0})
    r = h.check_products_priced("006X")
    assert r.status is False
    assert "priced" in r.reason.lower() or "product" in r.reason.lower()


def test_products_priced_fails_with_missing_prices(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "OLI1", "UnitPrice": 100},
            {"Id": "OLI2", "UnitPrice": None},
        ],
        "totalSize": 2,
    })
    r = h.check_products_priced("006X")
    assert r.status is False
    assert "missing" in r.reason.lower()


def test_products_priced_passes(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "OLI1", "UnitPrice": 100}],
        "totalSize": 1,
    })
    r = h.check_products_priced("006X")
    assert r.status is True


# ---------- stakeholders_captured ----------

def test_stakeholders_captured_requires_primary(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "OCR1", "IsPrimary": False}],
        "totalSize": 1,
    })
    r = h.check_stakeholders_captured("006X")
    assert r.status is False


def test_stakeholders_captured_passes(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "OCR1", "IsPrimary": True}],
        "totalSize": 1,
    })
    r = h.check_stakeholders_captured("006X")
    assert r.status is True


# ---------- contract_countersigned ----------

def test_contract_countersigned_completed(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "006X", "DocuSign_Status__c": "Completed"}],
        "totalSize": 1,
    })
    r = h.check_contract_countersigned("006X")
    assert r.status is True


def test_contract_countersigned_not_completed(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "006X", "DocuSign_Status__c": "Sent"}],
        "totalSize": 1,
    })
    r = h.check_contract_countersigned("006X")
    assert r.status is False


# ---------- zenskar_billing ----------

def test_zenskar_informational_by_default(monkeypatch):
    monkeypatch.delenv("ONBOARDING_ZENSKAR_GATE_ACTIVE", raising=False)
    r = h.check_zenskar_billing("006X")
    assert r.status is None
    assert "pending" in r.reason.lower()


def test_zenskar_active_flag_still_deferred(monkeypatch):
    monkeypatch.setenv("ONBOARDING_ZENSKAR_GATE_ACTIVE", "1")
    r = h.check_zenskar_billing("006X")
    assert r.status is None  # still informational until real check lands


# ---------- kickoff_on_calendar ----------

@pytest.mark.parametrize("value,expected",
                         [("Kickoff Scheduled", True),
                          ("Kickoff Held", True),
                          ("Not Scheduled", False),
                          (None, False)])
def test_kickoff_on_calendar(fake_sf_monkeypatch, value, expected):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "a01X", "Kickoff_Status__c": value}],
        "totalSize": 1,
    })
    r = h.check_kickoff_on_calendar("006X")
    assert (r.status is True) == expected


# ---------- implementation_plan_attached ----------

def test_implementation_plan_present(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [{"Id": "CDL1",
                     "ContentDocument": {"Title": "Implementation Plan v1"}}],
        "totalSize": 1,
    })
    r = h.check_implementation_plan_attached("006X", "a01X")
    assert r.status is True


def test_implementation_plan_missing(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({"records": [], "totalSize": 0})
    r = h.check_implementation_plan_attached("006X", None)
    assert r.status is False


# ---------- summarize + format ----------

def test_summarize_counts_all_states():
    results = [
        h.CheckResult("a", True, "ok"),
        h.CheckResult("b", True, "ok"),
        h.CheckResult("c", False, "nope"),
        h.CheckResult("d", None, "info"),
    ]
    s = h.summarize(results)
    assert s["passed"] == 2
    assert s["failed"] == 1
    assert s["informational"] == 1
    assert s["all_pass"] is False


def test_format_slack_includes_emojis_and_blocker_hint():
    out = h.format_slack(
        "Acme",
        [
            h.CheckResult("a", True, "ok"),
            h.CheckResult("b", False, "missing"),
        ],
    )
    assert "Acme" in out
    assert "✅" in out
    assert "❌" in out
    assert "skip" in out.lower()


def test_checks_tuple_is_six_items():
    """Seed set is frozen at 6 per Q3 resolution."""
    assert len(h.CHECKS) == 6


def test_run_returns_six_results(fake_sf_monkeypatch, monkeypatch):
    # Cheap: return an empty record set for every SOQL query.
    fake_sf_monkeypatch._queues["soql_query"] = [
        {"records": [], "totalSize": 0} for _ in range(20)
    ]
    monkeypatch.delenv("ONBOARDING_ZENSKAR_GATE_ACTIVE", raising=False)
    results = h.run("006X", "a01X")
    assert len(results) == 6
