"""Tests for data_quality.bulk_updater."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text


@pytest.fixture
def _fresh_governance_state():
    """Wipe approval_gates / audit_log / rate_limits so each test starts clean."""
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'revops_support'"))
        conn.execute(
            text("DELETE FROM approval_gates WHERE agent_name = 'revops_support'")
        )
        conn.execute(
            text("DELETE FROM rate_limits WHERE bucket = 'revops_bulk_update_daily'")
        )
    yield
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'revops_support'"))
        conn.execute(
            text("DELETE FROM approval_gates WHERE agent_name = 'revops_support'")
        )
        conn.execute(
            text("DELETE FROM rate_limits WHERE bucket = 'revops_bulk_update_daily'")
        )


def _approved_gate(action_type: str) -> int:
    """Insert an already-approved gate so require_approved_gate() passes."""
    from shared.db.connection import get_engine
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at) "
                "VALUES ('revops_support', :act, '{}', 'test', 'O', "
                " 'approved', :rq, 'O', :dc)"
            ),
            {"act": action_type, "rq": now, "dc": now},
        )
        gate_id = result.lastrowid
        if gate_id is None:
            gate_id = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()[0]
        return int(gate_id)


class _FakeResponse:
    def __init__(self, payload: list[dict[str, Any]], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _make_fake_patch(*, call_log: list[dict[str, Any]], per_record_results=None):
    def fake_patch(url, *, headers=None, json=None, timeout=None):
        call_log.append({"url": url, "headers": headers, "json": json})
        records = json["records"] if json else []
        if per_record_results is not None:
            return _FakeResponse(per_record_results(records))
        return _FakeResponse(
            [{"id": r.get("Id"), "success": True, "errors": []} for r in records]
        )

    return fake_patch


def _fake_soql_factory(existing: dict[str, dict[str, Any]]):
    def fake_soql(query, limit=100):
        records = [
            {"Id": rid, **fields} for rid, fields in existing.items() if rid in query
        ]
        return {"records": records}

    return fake_soql


def _fake_auth():
    return "tok-xyz", "https://example.my.salesforce.com"


# ---------- validation + gate enforcement ----------


def test_empty_updates_rejected(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("single_record_update")
    with pytest.raises(bulk_updater.BulkUpdateError):
        bulk_updater.bulk_update("Account", [], approval_gate_id=gate)


def test_missing_id_rejected(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("bulk_update_small")
    with pytest.raises(bulk_updater.BulkUpdateError):
        bulk_updater.bulk_update(
            "Account",
            [{"Id": "001x", "Name": "A"}, {"Name": "No-Id"}],
            approval_gate_id=gate,
        )


def test_rejects_without_gate(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater
    from shared.governance import ApprovalRequired

    with pytest.raises(ApprovalRequired):
        bulk_updater.bulk_update(
            "Account",
            [{"Id": "001x", "Name": "A"}],
            approval_gate_id=None,  # type: ignore[arg-type]
        )


def test_gate_action_type_mismatch_raises(_fresh_governance_state):
    """Gate approved for single_record_update cannot cover a 5-row bulk."""
    from agents.revops_support.data_quality import bulk_updater
    from shared.governance import ApprovalRequired

    wrong = _approved_gate("single_record_update")
    updates = [{"Id": f"001{i:06d}", "Phone": "555"} for i in range(5)]
    with pytest.raises(ApprovalRequired):
        bulk_updater.bulk_update("Account", updates, approval_gate_id=wrong)


# ---------- end-to-end ----------


def test_runs_and_writes_audit(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("bulk_update_small")

    existing = {
        "001AAAA": {"Phone": "111"},
        "001BBBB": {"Phone": "222"},
    }
    updates = [
        {"Id": "001AAAA", "Phone": "999"},
        {"Id": "001BBBB", "Phone": "888"},
    ]

    calls: list[dict[str, Any]] = []
    runner = bulk_updater.BulkUpdater(
        http_patch=_make_fake_patch(call_log=calls),
        soql_query=_fake_soql_factory(existing),
        auth_resolver=_fake_auth,
    )
    result = runner.run("Account", updates, approval_gate_id=gate)

    assert result.total == 2
    assert result.success == 2
    assert result.failures == []
    assert result.before_snapshot == existing
    assert len(calls) == 1
    assert calls[0]["url"].endswith("/composite/sobjects")
    assert calls[0]["headers"]["Authorization"] == "Bearer tok-xyz"
    sent = calls[0]["json"]
    assert sent["allOrNone"] is False
    assert sent["records"][0]["attributes"] == {"type": "Account"}
    assert sent["records"][0]["Id"] == "001AAAA"

    # Audit row was written with before_value containing old Phone values.
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT before_value, after_value, action, approval_gate_id "
                "FROM audit_log WHERE agent_name = 'revops_support' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    before = json.loads(row[0])
    assert before["001AAAA"]["Phone"] == "111"
    assert before["001BBBB"]["Phone"] == "222"
    assert row[2] == "sf_bulk_update"
    assert row[3] == gate


def test_chunks_at_200(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("bulk_update_large")
    updates = [{"Id": f"001{i:06d}", "Phone": "999"} for i in range(250)]
    existing = {u["Id"]: {"Phone": "000"} for u in updates}

    calls: list[dict[str, Any]] = []
    runner = bulk_updater.BulkUpdater(
        http_patch=_make_fake_patch(call_log=calls),
        soql_query=_fake_soql_factory(existing),
        auth_resolver=_fake_auth,
    )
    result = runner.run("Account", updates, approval_gate_id=gate)

    assert result.total == 250
    assert result.success == 250
    # 2 chunks: 200 then 50
    assert len(calls) == 2
    assert len(calls[0]["json"]["records"]) == 200
    assert len(calls[1]["json"]["records"]) == 50


def test_partial_failure_recorded(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("bulk_update_small")
    updates = [{"Id": "001AAAA", "Phone": "999"}, {"Id": "001BBBB", "Phone": "888"}]
    existing = {"001AAAA": {"Phone": "111"}, "001BBBB": {"Phone": "222"}}

    def per_record(records):
        return [
            {"id": "001AAAA", "success": True, "errors": []},
            {"id": "001BBBB", "success": False,
             "errors": [{"statusCode": "INVALID_FIELD", "message": "bad"}]},
        ]

    runner = bulk_updater.BulkUpdater(
        http_patch=_make_fake_patch(call_log=[], per_record_results=per_record),
        soql_query=_fake_soql_factory(existing),
        auth_resolver=_fake_auth,
    )
    result = runner.run("Account", updates, approval_gate_id=gate)

    assert result.success == 1
    assert len(result.failures) == 1
    assert result.failures[0]["id"] == "001BBBB"
    assert result.failures[0]["errors"][0]["statusCode"] == "INVALID_FIELD"


def test_dry_run_skips_write(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("bulk_update_small")
    existing = {"001AAAA": {"Phone": "111"}, "001BBBB": {"Phone": "222"}}
    updates = [
        {"Id": "001AAAA", "Phone": "999"},
        {"Id": "001BBBB", "Phone": "888"},
    ]

    calls: list[dict[str, Any]] = []
    runner = bulk_updater.BulkUpdater(
        http_patch=_make_fake_patch(call_log=calls),
        soql_query=_fake_soql_factory(existing),
        auth_resolver=_fake_auth,
    )
    result = runner.run("Account", updates, approval_gate_id=gate, dry_run=True)

    assert result.total == 2
    assert result.success == 0
    assert result.before_snapshot == existing
    assert calls == []  # no HTTP call

    # No audit row either — only real writes audit.
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE agent_name = 'revops_support' AND action = 'sf_bulk_update'"
            )
        ).fetchone()
    assert rows[0] == 0


def test_composite_http_error_bubbles(_fresh_governance_state):
    from agents.revops_support.data_quality import bulk_updater

    gate = _approved_gate("bulk_update_small")
    existing = {"001AAAA": {"Phone": "111"}, "001BBBB": {"Phone": "222"}}
    updates = [
        {"Id": "001AAAA", "Phone": "999"},
        {"Id": "001BBBB", "Phone": "888"},
    ]

    def bad_patch(url, *, headers=None, json=None, timeout=None):
        return _FakeResponse([{"error": "boom"}], status_code=500)

    runner = bulk_updater.BulkUpdater(
        http_patch=bad_patch,
        soql_query=_fake_soql_factory(existing),
        auth_resolver=_fake_auth,
    )
    with pytest.raises(bulk_updater.BulkUpdateError):
        runner.run("Account", updates, approval_gate_id=gate)


def test_rate_limit_enforced(_fresh_governance_state, monkeypatch):
    """Once daily bucket is at cap, next call raises before any write."""
    from agents.revops_support.data_quality import bulk_updater
    from shared.governance import RateLimitExceeded
    from shared.db.connection import get_engine

    # Pre-fill the bucket to cap (500) for today's window.
    now = datetime.now(timezone.utc)
    window = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rate_limits (bucket, count, window_start, limit_value) "
                "VALUES ('revops_bulk_update_daily', 500, :w, 500)"
            ),
            {"w": window},
        )

    gate = _approved_gate("bulk_update_small")
    updates = [
        {"Id": "001AAAA", "Phone": "999"},
        {"Id": "001BBBB", "Phone": "888"},
    ]
    existing = {"001AAAA": {"Phone": "111"}, "001BBBB": {"Phone": "222"}}
    calls: list[dict[str, Any]] = []
    runner = bulk_updater.BulkUpdater(
        http_patch=_make_fake_patch(call_log=calls),
        soql_query=_fake_soql_factory(existing),
        auth_resolver=_fake_auth,
    )
    with pytest.raises(RateLimitExceeded):
        runner.run("Account", updates, approval_gate_id=gate)
    assert calls == []  # never got to write
