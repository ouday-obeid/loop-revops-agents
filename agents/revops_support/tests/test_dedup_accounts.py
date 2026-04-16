"""Tests for data_quality.dedup_accounts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.revops_support.data_quality import dedup_accounts as da


@pytest.fixture
def _fresh_state():
    from shared.db.connection import get_engine
    def _wipe():
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM audit_log WHERE agent_name = 'revops_support' "
                    "AND action = 'dedup_accounts_poll'"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                    "AND category = 'account_dedup_review'"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM approval_gates WHERE agent_name = 'revops_support' "
                    "AND justification = 'test-acc-dedup'"
                )
            )
    _wipe()
    yield
    _wipe()


def _account(**o: Any) -> dict[str, Any]:
    base = {
        "Id": "001000000000001",
        "Name": "Acme Inc.",
        "Website": "acme.com",
        "BillingCity": "New York",
        "BillingState": "NY",
        "BillingCountry": "USA",
        "CreatedDate": "2024-01-01T00:00:00Z",
        "LastActivityDate": "2026-04-01",
        "OwnerId": "005000000000001",
        "Opportunities": {"records": []},
    }
    base.update(o)
    return base


def _approved_account_merge_gate() -> int:
    from shared.db.connection import get_engine
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        r = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at) "
                "VALUES ('revops_support', 'account_merge', '{}', 'test-acc-dedup', "
                " 'O', 'approved', :n, 'O', :n)"
            ),
            {"n": now},
        )
        gid = r.lastrowid
        if gid is None:
            gid = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()[0]
        return int(gid)


def _mk_soql(records):
    def q(_query, limit=5000):
        return {"records": records}
    return q


def test_normalize_name_strips_suffixes_and_punct():
    assert da.normalize_name("Acme, Inc.") == "acme"
    assert da.normalize_name("The Acme Corporation") == "acme"
    assert da.normalize_name("A.C.M.E. LLC") == "acme"
    assert da.normalize_name(None) == ""


def test_extract_domain_strips_scheme_and_www():
    assert da.extract_domain("https://www.acme.com/about") == "acme.com"
    assert da.extract_domain("http://acme.com?utm=x") == "acme.com"
    assert da.extract_domain(None) is None


def test_scan_accounts_counts_opportunities():
    accts = da.scan_accounts(soql_query=_mk_soql([
        _account(Id="A1", Opportunities={"records": [{"Id": "O1"}, {"Id": "O2"}]}),
    ]))
    assert accts[0]["opp_count"] == 2
    assert accts[0]["domain"] == "acme.com"


def test_cluster_requires_name_AND_secondary_match():
    # Same name + same domain → cluster
    accts = [
        {"id": "A1", "name": "Acme Inc", "normalized_name": "acme",
         "domain": "acme.com", "city": "NYC", "state": "NY"},
        {"id": "A2", "name": "ACME", "normalized_name": "acme",
         "domain": "acme.com", "city": "SF", "state": "CA"},
    ]
    clusters = da.cluster_accounts(accts)
    assert len(clusters) == 1


def test_cluster_splits_same_name_different_domain_and_city():
    accts = [
        {"id": "A1", "name": "Acme", "normalized_name": "acme",
         "domain": "acme-east.com", "city": "NYC", "state": "NY"},
        {"id": "A2", "name": "Acme", "normalized_name": "acme",
         "domain": "acme-west.com", "city": "SF", "state": "CA"},
    ]
    # Different domain AND different city → no cluster
    clusters = da.cluster_accounts(accts)
    assert clusters == []


def test_cluster_matches_on_city_state_when_no_domain():
    accts = [
        {"id": "A1", "name": "Acme", "normalized_name": "acme",
         "domain": None, "city": "Austin", "state": "TX"},
        {"id": "A2", "name": "Acme", "normalized_name": "acme",
         "domain": None, "city": "Austin", "state": "TX"},
    ]
    clusters = da.cluster_accounts(accts)
    assert len(clusters) == 1


def test_cluster_ignores_empty_normalized_name():
    accts = [
        {"id": "A1", "name": "", "normalized_name": "", "domain": "x.com",
         "city": "NYC", "state": "NY"},
        {"id": "A2", "name": "", "normalized_name": "", "domain": "x.com",
         "city": "NYC", "state": "NY"},
    ]
    assert da.cluster_accounts(accts) == []


def test_propose_merges_master_has_most_opps():
    cluster = {"normalized_name": "acme", "accounts": [
        {"id": "A1", "opp_count": 0, "last_activity_date": "2026-04-15", "created_date": "2024-01-01T00:00:00Z"},
        {"id": "A2", "opp_count": 5, "last_activity_date": "2026-01-01", "created_date": "2025-01-01T00:00:00Z"},
    ]}
    props = da.propose_merges([cluster])
    assert props[0]["master_id"] == "A2"
    assert props[0]["duplicate_ids"] == ["A1"]


def test_apply_merge_batches_past_two():
    calls: list[dict[str, Any]] = []
    def fake_merge(sobject, master_id, duplicate_ids, *, agent_name, approval_gate_id):
        calls.append({"sobject": sobject, "dupes": duplicate_ids})
        return {"success": True}
    proposal = {"master_id": "M", "duplicate_ids": ["D1", "D2", "D3"]}
    da.apply_merge(proposal, approval_gate_id=9, merge_fn=fake_merge)
    assert [c["dupes"] for c in calls] == [["D1", "D2"], ["D3"]]
    assert all(c["sobject"] == "Account" for c in calls)


def test_poll_creates_tasks_and_audit(_fresh_state):
    records = [
        _account(Id="A1", Name="Acme Inc.", Website="acme.com",
                 BillingCity="NYC", BillingState="NY"),
        _account(Id="A2", Name="Acme Corp", Website="acme.com",
                 BillingCity="SF", BillingState="CA"),
    ]
    result = da.poll(soql_query=_mk_soql(records))
    assert len(result["clusters"]) == 1
    assert len(result["proposals"]) == 1
    assert len(result["task_ids"]) == 1


def test_poll_dedupes_tasks_across_runs(_fresh_state):
    records = [
        _account(Id="A1", Website="acme.com"),
        _account(Id="A2", Name="ACME", Website="acme.com"),
    ]
    da.poll(soql_query=_mk_soql(records))
    second = da.poll(soql_query=_mk_soql(records))
    assert second["task_ids"] == []


def test_poll_repair_requires_gate():
    records = [
        _account(Id="A1", Website="acme.com"),
        _account(Id="A2", Name="ACME", Website="acme.com"),
    ]
    with pytest.raises(ValueError, match="approval_gate_id"):
        da.poll(repair_=True, soql_query=_mk_soql(records))


def test_poll_repair_executes_merges(_fresh_state):
    gate = _approved_account_merge_gate()
    calls: list[dict[str, Any]] = []
    def fake_merge(sobject, master_id, duplicate_ids, *, agent_name, approval_gate_id):
        calls.append({"master": master_id, "dupes": duplicate_ids})
        return {"success": True}

    records = [
        _account(
            Id="A1", Website="acme.com",
            Opportunities={"records": [{"Id": "O1"}]},
        ),
        _account(Id="A2", Name="ACME", Website="acme.com"),
    ]
    result = da.poll(
        repair_=True, approval_gate_id=gate,
        soql_query=_mk_soql(records), merge_fn=fake_merge,
    )
    assert len(result["merges"]) == 1
    # A1 wins master (1 opp vs 0)
    assert calls[0]["master"] == "A1"


def test_dispatcher_routes_dedup_accounts():
    import asyncio
    from unittest.mock import patch
    from agents.revops_support.agent import RevOpsSupportAgent

    agent = RevOpsSupportAgent()
    with patch.object(da, "poll", return_value={
        "clusters": [], "proposals": [], "task_ids": [], "merges": [],
    }) as mocked:
        resp = asyncio.run(agent.handle("", {"text": "dedup accounts"}))
    assert mocked.called
    assert "account" in resp["text"].lower()
