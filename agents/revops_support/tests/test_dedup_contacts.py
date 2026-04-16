"""Tests for data_quality.dedup_contacts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.revops_support.data_quality import dedup_contacts as dc


@pytest.fixture
def _fresh_state():
    from shared.db.connection import get_engine
    def _wipe():
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM audit_log WHERE agent_name = 'revops_support' "
                    "AND action = 'dedup_contacts_poll'"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                    "AND category = 'contact_dedup_review'"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM approval_gates WHERE agent_name = 'revops_support' "
                    "AND justification = 'test-dedup'"
                )
            )
    _wipe()
    yield
    _wipe()


def _contact(**o: Any) -> dict[str, Any]:
    base = {
        "Id": "003000000000001",
        "Name": "Jane Doe",
        "Email": "jane@x.com",
        "AccountId": "001000000000001",
        "OwnerId": "005000000000001",
        "CreatedDate": "2024-01-01T00:00:00Z",
        "LastActivityDate": "2026-04-01",
        "LastModifiedDate": "2026-04-01T00:00:00Z",
        "Account": {"Name": "Acme"},
    }
    base.update(o)
    return base


def _approved_merge_gate() -> int:
    from shared.db.connection import get_engine
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        r = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at) "
                "VALUES ('revops_support', 'contact_merge', '{}', 'test-dedup', 'O', "
                " 'approved', :n, 'O', :n)"
            ),
            {"n": now},
        )
        gid = r.lastrowid
        if gid is None:
            gid = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()[0]
        return int(gid)


def _mk_soql(emails_records, detail_records):
    def q(query, limit=2000):
        if "GROUP BY Email" in query:
            return {"records": emails_records}
        return {"records": detail_records}
    return q


def test_scan_clusters_groups_duplicates():
    clusters = dc.scan_clusters(soql_query=_mk_soql(
        emails_records=[{"Email": "jane@x.com", "c": 2}],
        detail_records=[
            _contact(Id="C1"),
            _contact(Id="C2", Name="Jane D."),
        ],
    ))
    assert len(clusters) == 1
    assert clusters[0]["email"] == "jane@x.com"
    assert len(clusters[0]["contacts"]) == 2


def test_scan_clusters_returns_empty_when_no_dupes():
    clusters = dc.scan_clusters(soql_query=_mk_soql(
        emails_records=[],
        detail_records=[],
    ))
    assert clusters == []


def test_scan_clusters_filters_singletons_after_fetch():
    # Detail query returns 1 record for an email that appeared in the GROUP BY
    # (race between the two queries). The cluster builder drops it.
    clusters = dc.scan_clusters(soql_query=_mk_soql(
        emails_records=[{"Email": "ghost@x.com", "c": 2}],
        detail_records=[_contact(Id="C1", Email="ghost@x.com")],
    ))
    assert clusters == []


def test_propose_merges_picks_master_with_account_over_none():
    clusters = [{
        "email": "jane@x.com",
        "contacts": [
            _c_shape(id_="C1", account_id=None, last_activity_date="2026-04-15"),
            _c_shape(id_="C2", account_id="001", last_activity_date="2026-01-01"),
        ],
    }]
    props = dc.propose_merges(clusters)
    assert len(props) == 1
    assert props[0]["master_id"] == "C2"
    assert props[0]["duplicate_ids"] == ["C1"]


def test_propose_merges_ties_break_on_last_activity():
    clusters = [{
        "email": "jane@x.com",
        "contacts": [
            _c_shape(id_="C1", account_id="001", last_activity_date="2026-01-01"),
            _c_shape(id_="C2", account_id="001", last_activity_date="2026-04-15"),
        ],
    }]
    props = dc.propose_merges(clusters)
    assert props[0]["master_id"] == "C2"


def test_propose_merges_prefers_oldest_when_activity_ties():
    clusters = [{
        "email": "jane@x.com",
        "contacts": [
            _c_shape(
                id_="C1", account_id="001",
                last_activity_date="2026-04-01",
                created_date="2024-01-01T00:00:00Z",
            ),
            _c_shape(
                id_="C2", account_id="001",
                last_activity_date="2026-04-01",
                created_date="2025-01-01T00:00:00Z",
            ),
        ],
    }]
    props = dc.propose_merges(clusters)
    assert props[0]["master_id"] == "C1"


def test_apply_merge_batches_past_two():
    """SF REST caps at 2 duplicates per call — we split the cluster."""
    calls: list[dict[str, Any]] = []

    def fake_merge(sobject, master_id, duplicate_ids, *, agent_name, approval_gate_id):
        calls.append({"master": master_id, "dupes": duplicate_ids, "gate": approval_gate_id})
        return {"success": True, "id": master_id}

    proposal = {
        "master_id": "M",
        "duplicate_ids": ["D1", "D2", "D3"],
    }
    results = dc.apply_merge(proposal, approval_gate_id=7, merge_fn=fake_merge)
    assert len(results) == 2
    assert [c["dupes"] for c in calls] == [["D1", "D2"], ["D3"]]
    assert all(c["gate"] == 7 for c in calls)


def test_poll_creates_tasks_and_audit(_fresh_state):
    clusters_records = [{"Email": "a@x.com", "c": 2}]
    details = [
        _contact(Id="C1", Email="a@x.com", AccountId="001"),
        _contact(Id="C2", Email="a@x.com", AccountId=None),
    ]
    result = dc.poll(soql_query=_mk_soql(clusters_records, details))
    assert len(result["clusters"]) == 1
    assert len(result["proposals"]) == 1
    assert len(result["task_ids"]) == 1

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        tasks = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'contact_dedup_review'"
            )
        ).scalar_one()
        audits = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log WHERE agent_name = 'revops_support' "
                "AND action = 'dedup_contacts_poll'"
            )
        ).scalar_one()
    assert tasks == 1
    assert audits == 1


def test_poll_dedupes_tasks_across_runs(_fresh_state):
    records = [{"Email": "a@x.com", "c": 2}]
    details = [_contact(Id="C1", Email="a@x.com"), _contact(Id="C2", Email="a@x.com")]
    dc.poll(soql_query=_mk_soql(records, details))
    second = dc.poll(soql_query=_mk_soql(records, details))
    assert second["task_ids"] == []


def test_poll_repair_requires_gate():
    with pytest.raises(ValueError, match="approval_gate_id"):
        # Give it something to repair so the branch triggers
        dc.poll(
            repair_=True,
            soql_query=_mk_soql(
                [{"Email": "a@x.com", "c": 2}],
                [_contact(Id="C1", Email="a@x.com"), _contact(Id="C2", Email="a@x.com")],
            ),
        )


def test_poll_repair_executes_merges(_fresh_state):
    gate = _approved_merge_gate()
    calls: list[dict[str, Any]] = []

    def fake_merge(sobject, master_id, duplicate_ids, *, agent_name, approval_gate_id):
        calls.append({"master": master_id, "dupes": duplicate_ids})
        return {"success": True, "id": master_id}

    records = [{"Email": "a@x.com", "c": 2}]
    details = [
        _contact(Id="C1", Email="a@x.com", AccountId="001"),
        _contact(Id="C2", Email="a@x.com", AccountId=None),
    ]
    result = dc.poll(
        repair_=True, approval_gate_id=gate,
        soql_query=_mk_soql(records, details), merge_fn=fake_merge,
    )
    assert len(result["merges"]) == 1
    assert calls == [{"master": "C1", "dupes": ["C2"]}]


def test_dispatcher_routes_dedup_contacts():
    import asyncio
    from unittest.mock import patch
    from agents.revops_support.agent import RevOpsSupportAgent

    agent = RevOpsSupportAgent()
    with patch.object(dc, "poll", return_value={
        "clusters": [], "proposals": [], "task_ids": [], "merges": [],
    }) as mocked:
        resp = asyncio.run(agent.handle("", {"text": "dedup contacts"}))
    assert mocked.called
    assert "contact" in resp["text"].lower()


def _c_shape(id_: str, account_id: str | None, last_activity_date: str, created_date: str = "2025-01-01T00:00:00Z"):
    return {
        "id": id_,
        "name": f"Contact {id_}",
        "email": "jane@x.com",
        "account_id": account_id,
        "owner_id": "005",
        "created_date": created_date,
        "last_activity_date": last_activity_date,
        "last_modified_date": "2026-04-01T00:00:00Z",
    }
