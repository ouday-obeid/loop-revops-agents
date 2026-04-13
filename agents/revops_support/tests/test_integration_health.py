"""Tests for the revops_support integration_health monitors."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text


@pytest.fixture
def _clean_tasks():
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'sf_integration_health'"
            )
        )
    yield
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'sf_integration_health'"
            )
        )


# ---------- flow_monitor ----------


def test_flow_monitor_detects_recent_failures(_clean_tasks):
    from agents.revops_support.integration_health import flow_monitor

    interviews = [
        {"Id": "301x1", "InterviewLabel": "Opp_Auto-001",
         "FlowDefinitionView": {"DeveloperName": "Opp_Auto"},
         "InterviewStatus": "Error", "CreatedDate": "2026-04-13T12:00:00Z"},
        {"Id": "301x2", "InterviewLabel": "Opp_Auto-002",
         "FlowDefinitionView": {"DeveloperName": "Opp_Auto"},
         "InterviewStatus": "Error", "CreatedDate": "2026-04-13T12:01:00Z"},
        {"Id": "301x3", "InterviewLabel": "Lead_Route-001",
         "FlowDefinitionView": {"DeveloperName": "Lead_Route"},
         "InterviewStatus": "Completed", "CreatedDate": "2026-04-13T12:02:00Z"},
    ]
    flows: list[dict[str, Any]] = [
        {"Id": "301a", "MasterLabel": "Opp_Auto", "DeveloperName": "Opp_Auto",
         "Status": "Active", "ProcessType": "AutoLaunchedFlow"},
    ]

    def fake_q(q, *_a, **_kw):
        return {"records": interviews if "FROM FlowInterview" in q else flows}

    out = flow_monitor.poll(soql_query=fake_q, tooling_query=fake_q)
    assert len(out) == 1
    assert out[0]["kind"] == "recent_failures"
    assert out[0]["flow_name"] == "Opp_Auto"
    assert out[0]["count"] == 2
    assert out[0]["task_id"] is not None


def test_flow_monitor_detects_obsolete_still_running(_clean_tasks):
    from agents.revops_support.integration_health import flow_monitor

    # Flow marked Obsolete but interview rows show it still firing.
    interviews = [
        {"Id": "301x1", "InterviewLabel": "ZombieFlow-1",
         "InterviewStatus": "Completed", "CreatedDate": "2026-04-13T12:00:00Z"},
        {"Id": "301x2", "InterviewLabel": "ZombieFlow-2",
         "InterviewStatus": "Completed", "CreatedDate": "2026-04-13T12:01:00Z"},
    ]
    flows = [
        {"Id": "301a", "MasterLabel": "ZombieFlow", "DeveloperName": "ZombieFlow",
         "Status": "Obsolete", "ProcessType": "AutoLaunchedFlow"},
    ]

    def fake_q(q, *_a, **_kw):
        return {"records": interviews if "FROM FlowInterview" in q else flows}

    out = flow_monitor.poll(soql_query=fake_q, tooling_query=fake_q)
    kinds = [p["kind"] for p in out]
    assert "obsolete_still_running" in kinds


def test_flow_monitor_task_is_idempotent(_clean_tasks):
    from agents.revops_support.integration_health import flow_monitor

    interviews = [
        {"Id": "e1", "InterviewLabel": "MyFlow-1",
         "FlowDefinitionView": {"DeveloperName": "MyFlow"},
         "InterviewStatus": "Error", "CreatedDate": "2026-04-13T12:00:00Z"},
    ]
    flows: list[dict[str, Any]] = []

    def fake_q(q, *_a, **_kw):
        return {"records": interviews if "FROM FlowInterview" in q else flows}

    first = flow_monitor.poll(soql_query=fake_q, tooling_query=fake_q)
    second = flow_monitor.poll(soql_query=fake_q, tooling_query=fake_q)
    assert first[0]["task_id"] == second[0]["task_id"]  # same task reused


# ---------- apex_job_monitor ----------


def test_apex_failures_surface_per_class(_clean_tasks):
    from agents.revops_support.integration_health import apex_job_monitor

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs = [
        {"Id": "707a", "Status": "Failed",
         "ApexClass": {"Name": "MyBatchJob"}, "CreatedDate": now},
        {"Id": "707b", "Status": "Failed",
         "ApexClass": {"Name": "MyBatchJob"}, "CreatedDate": now},
        {"Id": "707c", "Status": "Completed",
         "ApexClass": {"Name": "Healthy"}, "CreatedDate": now},
    ]

    def fake_tq(q):
        return {"records": jobs}

    out = apex_job_monitor.poll(tooling_query=fake_tq)
    failures = [p for p in out if p["kind"] == "failures"]
    assert len(failures) == 1
    assert failures[0]["class_name"] == "MyBatchJob"
    assert failures[0]["count"] == 2


def test_apex_queue_depth_task(_clean_tasks):
    from agents.revops_support.integration_health import apex_job_monitor

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    jobs = [
        {"Id": f"707{i:03d}", "Status": "Queued",
         "ApexClass": {"Name": "Q"}, "CreatedDate": now}
        for i in range(apex_job_monitor.QUEUE_DEPTH_WARN)
    ]

    def fake_tq(q):
        return {"records": jobs}

    out = apex_job_monitor.poll(tooling_query=fake_tq)
    qd = [p for p in out if p["kind"] == "queue_depth"]
    assert len(qd) == 1
    assert qd[0]["count"] == apex_job_monitor.QUEUE_DEPTH_WARN


# ---------- metadata_drift ----------


def test_metadata_drift_first_call_records_baseline(tmp_path, monkeypatch, _clean_tasks):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.integration_health import metadata_drift

    descs = {
        "Account": {"fields": [{"name": "Id", "type": "id"}, {"name": "Name", "type": "string"}]},
    }

    def fake_describe(name):
        return descs.get(name, {"fields": []})

    out = metadata_drift.poll(sobjects=("Account",), describe_fn=fake_describe)
    assert out == []
    assert metadata_drift._state_path().exists()


def test_metadata_drift_second_call_detects_change(tmp_path, monkeypatch, _clean_tasks):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.integration_health import metadata_drift

    first = {"Account": {"fields": [
        {"name": "Id", "type": "id"},
        {"name": "Name", "type": "string"},
        {"name": "OldField__c", "type": "string"},
    ]}}
    second = {"Account": {"fields": [
        {"name": "Id", "type": "id"},
        {"name": "Name", "type": "textarea"},  # type change
        {"name": "NewField__c", "type": "string"},  # added
        # OldField__c removed
    ]}}

    def describe_first(name):
        return first.get(name, {"fields": []})

    metadata_drift.poll(sobjects=("Account",), describe_fn=describe_first)

    def describe_second(name):
        return second.get(name, {"fields": []})

    drift = metadata_drift.poll(sobjects=("Account",), describe_fn=describe_second)
    assert len(drift) == 1
    row = drift[0]
    assert row.added == ["NewField__c"]
    assert row.removed == ["OldField__c"]
    assert any("Name:" in c for c in row.changed)

    # Task got opened in the category bucket.
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        cnt = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks "
                "WHERE source = 'revops_support:metadata_drift:Account'"
            )
        ).fetchone()[0]
    assert cnt == 1


# ---------- sync_checker ----------


def test_sync_checker_healthy_produces_no_tasks(_clean_tasks):
    from agents.revops_support.integration_health import sync_checker

    now = datetime.now(timezone.utc)
    fresh_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    def fake_soql(q, limit=1):
        return {"records": [{"latest": fresh_iso}]}

    # Shrink probes to one for isolation.
    probes = (sync_checker.Probe(
        integration="vitally", max_staleness_seconds=3600, soql="..."),)
    out = sync_checker.poll(soql_query=fake_soql, probes=probes)
    assert out == []


def test_sync_checker_stale_surfaces_task(_clean_tasks):
    from agents.revops_support.integration_health import sync_checker

    old = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    def fake_soql(q, limit=1):
        return {"records": [{"latest": old}]}

    probes = (sync_checker.Probe(
        integration="vitally", max_staleness_seconds=3600, soql="..."),)
    out = sync_checker.poll(soql_query=fake_soql, probes=probes)
    assert len(out) == 1
    assert out[0]["integration"] == "vitally"
    assert out[0]["status"] == "stale"
    assert out[0]["task_id"] is not None


def test_sync_checker_query_error_surfaces_error_task(_clean_tasks):
    from agents.revops_support.integration_health import sync_checker

    def bad_soql(q, limit=1):
        raise RuntimeError("sf down")

    probes = (sync_checker.Probe(
        integration="zenskar", max_staleness_seconds=86400, soql="..."),)
    out = sync_checker.poll(soql_query=bad_soql, probes=probes)
    assert len(out) == 1
    assert out[0]["status"] == "error"
