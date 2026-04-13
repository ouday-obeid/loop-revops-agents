"""Onboarding agent test fixtures + isolated DB bootstrap.

Sibling `tests/conftest.py` at the repo root is only auto-loaded for tests
under `<root>/tests/…`. Since our tests live under `<root>/agents/onboarding/
tests/…`, we bootstrap our own tempfile sqlite DB here (same pattern as
`agents/revops_support/tests/conftest.py`).

Fixtures provided beyond DB isolation:
  - `fake_sf`            : injectable stand-in for shared.mcp.salesforce_mcp.
  - `fake_sf_monkeypatch`: same, wired into the salesforce_mcp module attrs.
  - `ob_payload`         : factory for Slack payloads targeting the dispatcher.
  - `frozen_now`         : local freezegun-style clock for stall math.
  - `seed_gate`          : creates an approval_gates row for auto-approve flows.
  - `make_opp`           : factory for Closed-Won opp records.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import text as sql_text


@pytest.fixture(scope="session", autouse=True)
def _onboarding_isolate_db():
    tmp = tempfile.mkdtemp(prefix="onboarding_test_")
    db_file = Path(tmp) / "test.db"
    os.environ["REVOPS_REPO_ROOT"] = tmp
    os.environ["REVOPS_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["REVOPS_KNOWLEDGE_BACKEND"] = "chromadb_local"
    os.environ["REVOPS_SECRETS_BACKEND"] = "dotenv"
    os.environ["SLACK_DEV_GUARD"] = "0"
    os.environ.setdefault("SF_ORG_ALIAS", "salesops")

    from shared.db import connection as c
    c.reset_cache()
    from shared.db.connection import init_schema
    init_schema()
    yield


from shared.db.connection import get_engine


# ---------- Slack payload factory ----------

@pytest.fixture
def ob_payload():
    def _make(text: str = "", user: str = "U_TEST", channel: str = "C_TEST") -> dict:
        return {"text": text, "user": user, "channel": channel}
    return _make


# ---------- Fake Salesforce MCP ----------

class FakeSF:
    """In-memory stand-in for the bits of salesforce_mcp the onboarding agent uses.

    The agent calls: soql_query, describe_sobject, get_record, create_record,
    update_record. We record every call and let the test inject canned results
    via `.set_response(method, key, value)` or `.queue_response(method, value)`.
    """

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []
        self._responses: dict[str, dict] = {  # method -> key -> response
            "soql_query": {},
            "describe_sobject": {},
            "get_record": {},
        }
        self._queues: dict[str, list] = {
            "soql_query": [],
            "describe_sobject": [],
            "get_record": [],
        }
        self.created: list[dict[str, Any]] = []  # record of create_record calls
        self.updated: list[dict[str, Any]] = []
        self.create_record_result: dict[str, Any] | None = None

    # keyed responses
    def set_soql(self, query_contains: str, result: dict):
        self._responses["soql_query"][query_contains] = result

    def set_describe(self, sobject: str, result: dict):
        self._responses["describe_sobject"][sobject] = result

    def set_get_record(self, key: str, result: dict):
        self._responses["get_record"][key] = result

    # queue-style (useful when the same method is called repeatedly)
    def queue_soql(self, result: dict):
        self._queues["soql_query"].append(result)

    # exported callables — shape matches salesforce_mcp
    def soql_query(self, query: str, limit: int = 100):
        self.calls.append(("soql_query", (query, limit), {}))
        # Match most-recently-set needle first. Closed-won SOQL contains
        # multiple substrings that tests register as separate needles
        # (e.g. "Onboarding_Record_Created__c" for the field probe AND
        # "StageName = 'Closed Won'" for the actual CW query). Reverse
        # iteration mirrors the implicit override convention: the test sets
        # the field probe first, then the more specific CW needle.
        needles = list(self._responses["soql_query"].keys())
        for needle in reversed(needles):
            if needle in query:
                return self._responses["soql_query"][needle]
        if self._queues["soql_query"]:
            return self._queues["soql_query"].pop(0)
        return {"records": [], "totalSize": 0, "done": True}

    def describe_sobject(self, name: str):
        self.calls.append(("describe_sobject", (name,), {}))
        return self._responses["describe_sobject"].get(name, {"fields": []})

    def get_record(self, sobject: str, record_id: str):
        self.calls.append(("get_record", (sobject, record_id), {}))
        return self._responses["get_record"].get(f"{sobject}:{record_id}", {})

    def create_record(self, sobject: str, fields: dict, *, agent_name: str,
                      approval_gate_id: int | None = None):
        self.calls.append(
            ("create_record", (sobject, fields),
             {"agent_name": agent_name, "approval_gate_id": approval_gate_id})
        )
        self.created.append(
            {"sobject": sobject, "fields": dict(fields), "approval_gate_id": approval_gate_id}
        )
        return self.create_record_result or {
            "id": f"NEW_{sobject}_{uuid.uuid4().hex[:6]}",
            "success": True,
        }

    def update_record(self, sobject: str, record_id: str, fields: dict, *,
                      agent_name: str, approval_gate_id: int | None = None):
        self.calls.append(
            ("update_record", (sobject, record_id, fields),
             {"agent_name": agent_name, "approval_gate_id": approval_gate_id})
        )
        self.updated.append(
            {"sobject": sobject, "id": record_id, "fields": dict(fields),
             "approval_gate_id": approval_gate_id}
        )
        return {"id": record_id, "success": True}


@pytest.fixture
def fake_sf():
    """Plain FakeSF — tests that want to inject it manually."""
    return FakeSF()


@pytest.fixture
def fake_sf_monkeypatch(monkeypatch):
    """Replace salesforce_mcp module attributes with FakeSF methods.

    Usage:
        def test_x(fake_sf_monkeypatch):
            fake = fake_sf_monkeypatch
            fake.set_soql("StageName = 'Closed Won'", {"records": [...]})
            # agent code that imports from salesforce_mcp now hits `fake`
    """
    fake = FakeSF()
    from shared.mcp import salesforce_mcp
    monkeypatch.setattr(salesforce_mcp, "soql_query", fake.soql_query)
    monkeypatch.setattr(salesforce_mcp, "describe_sobject", fake.describe_sobject)
    monkeypatch.setattr(salesforce_mcp, "get_record", fake.get_record)
    monkeypatch.setattr(salesforce_mcp, "create_record", fake.create_record)
    monkeypatch.setattr(salesforce_mcp, "update_record", fake.update_record)
    return fake


# ---------- Approval gate seed ----------

@pytest.fixture
def seed_gate():
    """Insert an approval_gates row and return its id. Defaults to auto-approved
    onboarding_auto_create so that create_record calls in tests pass the MCP's
    require_approved_gate check (when tested against the real governance module).
    """
    created_ids: list[int] = []

    def _make(*, action_type: str = "single_record_update",
              status: str = "approved",
              agent_name: str = "onboarding",
              payload: dict | None = None) -> int:
        engine = get_engine()
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            result = conn.execute(
                sql_text(
                    """INSERT INTO approval_gates
                          (agent_name, action_type, payload, justification,
                           requested_by, status, requested_at, decided_at,
                           approved_by, expires_at)
                       VALUES (:a, :act, :p, NULL, 'system:test', :s, :n,
                               CASE WHEN :s = 'approved' THEN :n ELSE NULL END,
                               CASE WHEN :s = 'approved' THEN 'system:test' ELSE NULL END,
                               :exp)"""
                ),
                {
                    "a": agent_name,
                    "act": action_type,
                    "p": json.dumps(payload or {}),
                    "s": status,
                    "n": now,
                    "exp": now,
                },
            )
            gate_id = result.lastrowid
            if gate_id is None:
                gate_id = conn.execute(
                    sql_text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
                ).fetchone()[0]
            created_ids.append(int(gate_id))
            return int(gate_id)

    yield _make

    # Cleanup — keep the table trim across tests even though DB is session-scoped.
    if created_ids:
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                sql_text("DELETE FROM approval_gates WHERE id IN :ids").bindparams(
                    sql_text(":ids")
                ),
                {"ids": tuple(created_ids)},
            ) if False else None  # noop — leave rows; session teardown drops DB


# ---------- Simple clock fake ----------

class FrozenClock:
    def __init__(self, now: datetime):
        self._now = now

    def advance(self, **delta):
        from datetime import timedelta
        self._now = self._now + timedelta(**delta)

    def set(self, now: datetime):
        self._now = now

    def now(self, tz=None):
        return self._now if tz is None else self._now.astimezone(tz)


@pytest.fixture
def frozen_now():
    """Returns a FrozenClock seeded at 2026-04-13 12:00 UTC (Phase 3 kickoff).

    Tests that need `datetime.now()` control should pass `clock.now` into the
    unit under test rather than patching module-level datetime — keeps the
    seam narrow and makes the substitution obvious at the call site.
    """
    return FrozenClock(datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc))


# ---------- Convenience: sample opp factory ----------

@pytest.fixture
def make_opp():
    """Build a Closed Won opp record dict matching CLOSED_WON_STRATEGY_A shape."""
    def _make(
        opp_id: str | None = None,
        account_id: str = "001ACME000000000AAA",
        account_name: str = "Acme Restaurants",
        owner_id: str | None = "005OWNER00000000AAA",
        close_date: str = "2026-04-10",
        amount: float = 24000.0,
        name: str = "Acme — 2026 MSA",
    ) -> dict:
        return {
            "Id": opp_id or f"006{uuid.uuid4().hex[:15].upper()}",
            "AccountId": account_id,
            "Account": {"Name": account_name},
            "OwnerId": owner_id,
            "CloseDate": close_date,
            "Amount": amount,
            "Name": name,
        }
    return _make
