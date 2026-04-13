"""Pipeline hygiene — SOQL building, aggregation, Slack rendering, error degradation."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from agents.sales_reps import pipeline_hygiene as ph


# --------------------------------------------------------------- SOQL building

def test_soql_stale_includes_ae_when_provided():
    q = ph._soql_stale("ae@tryloop.ai", 14)
    assert "Owner.Email = 'ae@tryloop.ai'" in q
    assert "LastActivityDate <" in q


def test_soql_stale_omits_ae_when_none():
    q = ph._soql_stale(None, 14)
    # SELECT uses Owner.Email as a column — but no WHERE filter on it.
    assert "Owner.Email = " not in q


def test_soql_missing_next_step_targets_advanced_stages():
    q = ph._soql_missing_next_step(None)
    assert "NextStep = null OR NextStep = ''" in q
    assert "'Proposal'" in q
    assert "'Negotiation'" in q


def test_escape_handles_quotes():
    q = ph._soql_stale("ae'inject@loop.ai", 7)
    # Must escape the single quote.
    assert "ae\\'inject@loop.ai" in q


# --------------------------------------------------------------- aggregation

def _finding(**overrides) -> ph.HygieneFinding:
    base = dict(
        opportunity_id="0061",
        name="Deal 1",
        owner_email="ae@tryloop.ai",
        stage="Proposal",
        amount=10000.0,
        close_date="2026-04-20",
        issue="stale_activity",
        details="no activity",
    )
    base.update(overrides)
    return ph.HygieneFinding(**base)


def test_aggregate_groups_by_owner():
    findings = [
        _finding(owner_email="a@tryloop.ai", issue="stale_activity"),
        _finding(owner_email="a@tryloop.ai", issue="past_close"),
        _finding(owner_email="b@tryloop.ai", issue="missing_next_step"),
    ]
    report = ph._aggregate(findings, ae_filter=None)
    assert set(report.findings_by_ae.keys()) == {"a@tryloop.ai", "b@tryloop.ai"}
    assert report.totals_by_issue == {
        "stale_activity": 1, "past_close": 1, "missing_next_step": 1,
    }
    assert report.total_findings == 3


def test_aggregate_unassigned_key_for_null_owner():
    findings = [_finding(owner_email=None)]
    report = ph._aggregate(findings, ae_filter=None)
    assert "(unassigned)" in report.findings_by_ae


# --------------------------------------------------------------- rendering

def test_render_empty_report_success():
    report = ph.HygieneReport(generated_at="2026-04-13", ae_filter=None)
    out = ph._render_slack(report)
    assert "no issues" in out


def test_render_shows_totals_and_preview():
    findings = [_finding(owner_email="a@x.com", opportunity_id=f"006{i:03d}")
                for i in range(8)]
    report = ph._aggregate(findings, ae_filter=None)
    out = ph._render_slack(report, preview_per_ae=3)
    assert "Pipeline hygiene — all AEs" in out
    assert "a@x.com" in out
    assert "…and 5 more" in out


def test_render_respects_ae_filter_label():
    findings = [_finding(owner_email="ae@tryloop.ai")]
    report = ph._aggregate(findings, ae_filter="ae@tryloop.ai")
    out = ph._render_slack(report)
    assert "AE: ae@tryloop.ai" in out


# --------------------------------------------------------------- detectors (mocked)

def test_find_stale_builds_findings_from_soql():
    fake = {"records": [{
        "Id": "006X",
        "Name": "Acme",
        "StageName": "Demo",
        "Amount": 12000.0,
        "CloseDate": "2026-04-30",
        "LastActivityDate": "2026-03-01",
        "Owner": {"Email": "ae@tryloop.ai", "Name": "AE One"},
    }]}
    with patch.object(ph.salesforce_mcp, "soql_query", return_value=fake):
        out = ph._find_stale(None, 14)
    assert len(out) == 1
    assert out[0].issue == "stale_activity"
    assert out[0].owner_email == "ae@tryloop.ai"


def test_find_single_threaded_only_flags_one_or_zero_contacts():
    fake = {"records": [
        {
            "Id": "006A", "Name": "Solo", "StageName": "Proposal",
            "Amount": 5000, "CloseDate": "2026-05-01",
            "Owner": {"Email": "ae@x.com"},
            "OpportunityContactRoles": {"records": [{"Id": "cr1"}]},
        },
        {
            "Id": "006B", "Name": "MultiT", "StageName": "Proposal",
            "Amount": 5000, "CloseDate": "2026-05-01",
            "Owner": {"Email": "ae@x.com"},
            "OpportunityContactRoles": {"records": [{"Id": "cr1"}, {"Id": "cr2"}]},
        },
    ]}
    with patch.object(ph.salesforce_mcp, "soql_query", return_value=fake):
        out = ph._find_single_threaded(None)
    assert len(out) == 1
    assert out[0].opportunity_id == "006A"


# --------------------------------------------------------------- top-level run

def test_run_aggregates_all_detectors():
    stale_rec = {"records": [{
        "Id": "006STALE", "Name": "S", "StageName": "Demo",
        "Amount": 1, "CloseDate": "2026-05-01",
        "LastActivityDate": "2020-01-01",
        "Owner": {"Email": "ae@x.com"},
    }]}
    past_rec = {"records": [{
        "Id": "006PAST", "Name": "P", "StageName": "Proposal",
        "Amount": 2, "CloseDate": "2020-01-01",
        "Owner": {"Email": "ae@x.com"},
    }]}
    mns_rec = {"records": [{
        "Id": "006MNS", "Name": "M", "StageName": "Proposal",
        "Amount": 3, "CloseDate": "2026-05-01", "NextStep": None,
        "Owner": {"Email": "ae@x.com"},
    }]}
    single_rec = {"records": [{
        "Id": "006SIN", "Name": "Si", "StageName": "Negotiation",
        "Amount": 4, "CloseDate": "2026-05-01",
        "Owner": {"Email": "ae@x.com"},
        "OpportunityContactRoles": {"records": []},
    }]}

    # Each call to soql_query returns one of the four fakes in order.
    side = [stale_rec, mns_rec, past_rec, single_rec]
    with patch.object(ph.salesforce_mcp, "soql_query", side_effect=side):
        out = asyncio.run(ph.run(ae_filter=None))
    assert out["total_findings"] == 4
    assert set(out["totals_by_issue"].keys()) == {
        "stale_activity", "missing_next_step", "past_close", "single_threaded",
    }


def test_run_degrades_on_exception():
    with patch.object(ph.salesforce_mcp, "soql_query", side_effect=RuntimeError("SF down")):
        out = asyncio.run(ph.run(ae_filter=None))
    assert "failed" in out["text"].lower()
    assert "error" in out
