"""Tests for data_quality.bad_conversions."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.revops_support.data_quality import bad_conversions as bc


@pytest.fixture
def _fresh_state():
    from shared.db.connection import get_engine
    def _wipe():
        with get_engine().begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM audit_log WHERE agent_name = 'revops_support' "
                    "AND action = 'bad_conversions_poll'"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                    "AND category = 'bad_conversion_review'"
                )
            )
            conn.execute(
                text(
                    "DELETE FROM approval_gates WHERE agent_name = 'revops_support' "
                    "AND justification = 't'"
                )
            )
    _wipe()
    yield
    _wipe()


def _lead(**overrides: Any) -> dict[str, Any]:
    base = {
        "Id": "00Q000000000001",
        "Name": "Jane Doe",
        "Email": "jane@example.com",
        "Company": "Example Inc",
        "ConvertedAccountId": "001000000000001",
        "ConvertedContactId": "003000000000001",
        "ConvertedOpportunityId": "006000000000001",
        "ConvertedDate": "2026-04-10",
        "OwnerId": "005000000000001",
    }
    base.update(overrides)
    return base


def _mk_soql(records: list[dict[str, Any]]):
    def q(_query, limit: int = 2000):
        return {"records": records}
    return q


def test_scan_identifies_missing_opportunity():
    orphans = bc.scan(
        soql_query=_mk_soql([_lead(ConvertedOpportunityId=None)])
    )
    assert len(orphans) == 1
    assert orphans[0]["issues"] == ["no_opportunity"]


def test_scan_identifies_missing_account():
    orphans = bc.scan(
        soql_query=_mk_soql([_lead(ConvertedAccountId=None)])
    )
    assert orphans[0]["issues"] == ["no_account"]


def test_scan_identifies_missing_contact():
    orphans = bc.scan(
        soql_query=_mk_soql([_lead(ConvertedContactId=None)])
    )
    assert orphans[0]["issues"] == ["no_contact"]


def test_scan_multiple_issues_combined():
    orphans = bc.scan(
        soql_query=_mk_soql(
            [_lead(ConvertedAccountId=None, ConvertedOpportunityId=None)]
        )
    )
    assert set(orphans[0]["issues"]) == {"no_account", "no_opportunity"}


def test_scan_returns_empty_on_clean_data():
    # The SOQL WHERE clause means SF wouldn't return healthy leads, but if the
    # mock mistakenly does, scan still filters correctly.
    orphans = bc.scan(soql_query=_mk_soql([_lead()]))
    assert orphans == []


def test_poll_creates_tasks_and_audit(_fresh_state):
    records = [
        _lead(Id="L1", ConvertedOpportunityId=None),
        _lead(Id="L2", Name="Bob Ross", ConvertedAccountId=None),
    ]
    result = bc.poll(soql_query=_mk_soql(records))
    assert result["total"] == 2
    assert result["counts"]["no_opportunity"] == 1
    assert result["counts"]["no_account"] == 1
    assert len(result["task_ids"]) == 2

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        tasks = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'bad_conversion_review'"
            )
        ).scalar_one()
        audits = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log WHERE agent_name = 'revops_support' "
                "AND action = 'bad_conversions_poll'"
            )
        ).scalar_one()
    assert tasks == 2
    assert audits == 1


def test_poll_sets_high_priority_for_no_account(_fresh_state):
    records = [_lead(Id="L1", ConvertedAccountId=None)]
    bc.poll(soql_query=_mk_soql(records))
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        prio = conn.execute(
            text(
                "SELECT priority FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'bad_conversion_review' LIMIT 1"
            )
        ).scalar_one()
    assert prio == "high"


def test_poll_dedupes_across_runs(_fresh_state):
    records = [_lead(Id="L1", ConvertedOpportunityId=None)]
    bc.poll(soql_query=_mk_soql(records))
    second = bc.poll(soql_query=_mk_soql(records))
    assert second["task_ids"] == []
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        tasks = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'bad_conversion_review'"
            )
        ).scalar_one()
    assert tasks == 1


def test_repair_requires_repair_field():
    with pytest.raises(NotImplementedError, match="repair_field"):
        bc.repair([{"lead_id": "L1", "issues": ["no_opportunity"]}])


def test_repair_requires_approval_gate():
    with pytest.raises(ValueError, match="approval_gate_id"):
        bc.repair(
            [{"lead_id": "L1", "issues": ["no_opportunity"]}],
            repair_field="Description",
        )


def test_repair_uses_bulk_updater(_fresh_state):
    captured: dict[str, Any] = {}

    class _FakeResult:
        def __init__(self):
            self.sobject = "Lead"
            self.total = 1
            self.success = 1
            self.failures: list[Any] = []
            self.audit_ids: list[int] = []
            self.before_snapshot: dict[str, Any] = {}
        def to_summary(self):
            return {"total": self.total, "success": self.success}

    class _FakeUpdater:
        def __init__(self, *, agent_name: str):
            captured["agent_name"] = agent_name
        def run(self, sobject, updates, *, approval_gate_id, dry_run=False):
            captured["sobject"] = sobject
            captured["updates"] = updates
            captured["gate_id"] = approval_gate_id
            return _FakeResult()

    summary = bc.repair(
        [{"lead_id": "L1", "issues": ["no_opportunity"]}],
        repair_field="Description",
        approval_gate_id=42,
        bulk_updater_cls=_FakeUpdater,
    )
    assert summary == {"total": 1, "success": 1}
    assert captured["sobject"] == "Lead"
    assert captured["gate_id"] == 42
    assert captured["updates"][0]["Id"] == "L1"
    assert "DQ-REPAIR" in captured["updates"][0]["Description"]


def _approved_bulk_gate() -> int:
    from shared.db.connection import get_engine
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at) "
                "VALUES ('revops_support', 'bulk_update_small', '{}', 't', 'O', "
                " 'approved', :rq, 'O', :dc)"
            ),
            {"rq": now, "dc": now},
        )
        gid = result.lastrowid
        if gid is None:
            gid = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()[0]
        return int(gid)


def test_poll_repair_path_wires_summary(_fresh_state):
    class _FakeResult:
        def to_summary(self):
            return {"total": 1, "success": 1}

    class _FakeUpdater:
        def __init__(self, *, agent_name: str):
            pass
        def run(self, *_a, **_kw):
            return _FakeResult()

    from agents.revops_support.data_quality import bulk_updater as bu_mod
    import unittest.mock as _m
    gate_id = _approved_bulk_gate()
    with _m.patch.object(bu_mod, "BulkUpdater", _FakeUpdater):
        records = [_lead(Id="L1", ConvertedOpportunityId=None)]
        result = bc.poll(
            soql_query=_mk_soql(records),
            repair_=True,
            repair_field="Description",
            approval_gate_id=gate_id,
        )
    assert result["repair_summary"] == {"total": 1, "success": 1}


def test_dispatcher_routes_bad_conversions():
    import asyncio
    from unittest.mock import patch
    from agents.revops_support.agent import RevOpsSupportAgent

    agent = RevOpsSupportAgent()
    with patch.object(bc, "poll", return_value={
        "total": 0, "counts": {}, "orphans": [], "task_ids": [], "repair_summary": None,
    }) as mocked:
        resp = asyncio.run(agent.handle("", {"text": "bad conversions"}))
    assert mocked.called
    assert "conversion" in resp["text"].lower()
