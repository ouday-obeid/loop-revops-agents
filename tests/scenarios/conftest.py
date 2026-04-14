"""Shared fixtures for Phase 1 DoD scenario integration tests.

Each scenario walks a user-visible path across one or more Phase 1 specialists
in a fully mocked environment. This conftest provides:

  * `_scenarios_isolate_db` (session, autouse) — tempfile sqlite DB + base schema
    + slt_metrics migrations, mirroring the per-agent conftest pattern but
    covering all tables any scenario could touch in one pass. Necessary because
    pytest only auto-discovers conftests under each test's path; when pytest is
    invoked against `tests/scenarios/` the per-agent conftests don't fire.
  * `FakeSF` — thin in-memory stand-in reused from the onboarding test kit's
    shape (`soql_query`, `describe_sobject`, `get_record`, `create_record`,
    `update_record`). Scenarios register keyed responses by SOQL substring.
  * `sf_monkeypatch` — convenience: apply a FakeSF to `shared.mcp.salesforce_mcp`
    for the duration of one test.
  * `SlackCapture` + `slack_capture` — records `SlackSender.send` calls without
    hitting Bolt. Scenarios assert `channel`, `text`, `blocks`.
  * `make_opp` — Closed Won opp factory (copied from the onboarding fixture so
    scenarios can import it without pulling the per-agent conftest).

Scoping: these fixtures are intentionally low-ceremony. If a scenario needs
richer behavior (fake gcal, fake fireflies, fake apollo), it builds it locally
and keeps the boundary narrow.

Tied to soak constraint — 2026-04-15 main unlock. Tests must be branch-only.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


# ---------------------------------------------------------------------- DB boot


@pytest.fixture(scope="session", autouse=True)
def _scenarios_isolate_db():
    """Tempdir sqlite + base schema + slt_metrics migrations.

    Matches the onboarding/revops_support/slt_metrics conftest shape. We eagerly
    apply migrations 0004 + 0005 so scenarios that touch pipeline_snapshots or
    the rep-config table don't blow up on collection order.
    """
    tmp = tempfile.mkdtemp(prefix="scenarios_test_")
    db_file = Path(tmp) / "test.db"
    os.environ["REVOPS_REPO_ROOT"] = tmp
    os.environ["REVOPS_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["REVOPS_KNOWLEDGE_BACKEND"] = "chromadb_local"
    os.environ["REVOPS_SECRETS_BACKEND"] = "dotenv"
    os.environ["SLACK_DEV_GUARD"] = "0"
    os.environ.setdefault("SF_ORG_ALIAS", "salesops-sandbox")
    os.environ.setdefault("APOLLO_API_KEY", "test-apollo")
    os.environ.setdefault("CLAY_API_KEY", "test-clay")
    os.environ.setdefault("CLAY_MONTHLY_BUDGET_CREDITS", "10000")
    os.environ.setdefault("AGENT_SF_USER_TOF", "tof-agent@tryloop.ai")

    from shared.db import connection as c

    c.reset_cache()
    from shared.db.connection import init_schema

    init_schema()

    # Apply slt_metrics migrations so pipeline_snapshots exists for Scenario 3.
    for mod_name in (
        "shared.db.migrations.versions.0004_slt_revenue_metrics",
        "shared.db.migrations.versions.0005_slt_rep_config",
    ):
        try:
            m = importlib.import_module(mod_name)
            m.upgrade()
        except Exception:  # pragma: no cover — migrations may already be applied
            pass
    yield


# ---------------------------------------------------------------------- FakeSF


class FakeSF:
    """In-memory SF stand-in shaped to match shared.mcp.salesforce_mcp.

    Usage:
        fake = FakeSF()
        fake.set_soql("StageName = 'Closed Won'", {"records": [...]})
        fake.set_describe("Opportunity", {"fields": [{"name": "StageName", ...}]})

    Matches by "needle in query" — most recently registered needle wins (mirrors
    the onboarding fixture convention).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self._responses: dict[str, dict] = {
            "soql_query": {},
            "describe_sobject": {},
            "get_record": {},
        }
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.create_record_result: dict[str, Any] | None = None

    def set_soql(self, query_contains: str, result: dict[str, Any]) -> None:
        self._responses["soql_query"][query_contains] = result

    def set_describe(self, sobject: str, result: dict[str, Any]) -> None:
        self._responses["describe_sobject"][sobject] = result

    def set_get_record(self, key: str, result: dict[str, Any]) -> None:
        self._responses["get_record"][key] = result

    def soql_query(self, query: str, limit: int = 100, *, intent: str = "read"):
        self.calls.append(("soql_query", (query, limit), {"intent": intent}))
        needles = list(self._responses["soql_query"].keys())
        for needle in reversed(needles):
            if needle in query:
                return self._responses["soql_query"][needle]
        return {"records": [], "totalSize": 0, "done": True}

    def describe_sobject(self, name: str, *, intent: str = "read"):
        self.calls.append(("describe_sobject", (name,), {"intent": intent}))
        return self._responses["describe_sobject"].get(name, {"fields": []})

    def get_record(self, sobject: str, record_id: str, *, intent: str = "read"):
        self.calls.append(("get_record", (sobject, record_id), {"intent": intent}))
        return self._responses["get_record"].get(f"{sobject}:{record_id}", {})

    def create_record(
        self,
        sobject: str,
        fields: dict[str, Any],
        *,
        agent_name: str,
        approval_gate_id: int | None = None,
        intent: str = "write",
    ):
        self.calls.append(
            (
                "create_record",
                (sobject, fields),
                {
                    "agent_name": agent_name,
                    "approval_gate_id": approval_gate_id,
                    "intent": intent,
                },
            )
        )
        self.created.append(
            {
                "sobject": sobject,
                "fields": dict(fields),
                "approval_gate_id": approval_gate_id,
            }
        )
        return self.create_record_result or {
            "id": f"NEW_{sobject}_{uuid.uuid4().hex[:6]}",
            "success": True,
        }

    def update_record(
        self,
        sobject: str,
        record_id: str,
        fields: dict[str, Any],
        *,
        agent_name: str,
        approval_gate_id: int | None = None,
        intent: str = "write",
    ):
        self.calls.append(
            (
                "update_record",
                (sobject, record_id, fields),
                {
                    "agent_name": agent_name,
                    "approval_gate_id": approval_gate_id,
                    "intent": intent,
                },
            )
        )
        self.updated.append(
            {
                "sobject": sobject,
                "id": record_id,
                "fields": dict(fields),
                "approval_gate_id": approval_gate_id,
            }
        )
        return {"id": record_id, "success": True}


@pytest.fixture
def fake_sf() -> FakeSF:
    """A fresh FakeSF — tests that want to inject it manually."""
    return FakeSF()


@pytest.fixture
def sf_monkeypatch(monkeypatch):
    """Replace shared.mcp.salesforce_mcp read+write surface with a FakeSF.

    Returns the FakeSF so the test can register responses + inspect calls.
    """
    fake = FakeSF()
    from shared.mcp import salesforce_mcp

    monkeypatch.setattr(salesforce_mcp, "soql_query", fake.soql_query)
    monkeypatch.setattr(salesforce_mcp, "describe_sobject", fake.describe_sobject)
    monkeypatch.setattr(salesforce_mcp, "get_record", fake.get_record)
    monkeypatch.setattr(salesforce_mcp, "create_record", fake.create_record)
    monkeypatch.setattr(salesforce_mcp, "update_record", fake.update_record)
    return fake


# ---------------------------------------------------------------- Slack capture


@dataclass
class SlackCapture:
    """Capture every SlackSender.send call without touching Bolt."""

    sent: list[dict[str, Any]] = field(default_factory=list)

    def send(
        self,
        channel: str,
        text: str,
        blocks: list[dict[str, Any]] | None = None,
        *,
        thread_ts: str | None = None,
    ) -> dict[str, Any]:
        self.sent.append(
            {"channel": channel, "text": text, "blocks": blocks, "thread_ts": thread_ts}
        )
        return {"ok": True, "ts": f"{len(self.sent)}.0", "channel": channel}

    # --- assertion helpers -------------------------------------------------

    def channels(self) -> set[str]:
        return {s["channel"] for s in self.sent}

    def find(self, *, channel: str | None = None, contains: str | None = None):
        out = []
        for s in self.sent:
            if channel is not None and s["channel"] != channel:
                continue
            if contains is not None and contains not in (s["text"] or ""):
                continue
            out.append(s)
        return out


@pytest.fixture
def slack_capture(monkeypatch) -> SlackCapture:
    """Monkeypatches SlackSender so any agent that tries to post is captured."""
    capture = SlackCapture()

    class _Captured:
        def __init__(self, client: Any | None = None):
            self._capture = capture

        def send(self, channel, text, blocks=None, *, thread_ts=None):
            return capture.send(channel, text, blocks, thread_ts=thread_ts)

    from shared import slack_dispatcher

    monkeypatch.setattr(slack_dispatcher, "SlackSender", _Captured)
    return capture


# ---------------------------------------------------------------- Opp factory


@pytest.fixture
def make_opp():
    """Build a Closed Won opp dict in the shape ToF/Onboarding/CS expect.

    Copied from agents/onboarding/tests/conftest.py::make_opp so scenarios can
    import without pulling the per-agent conftest.
    """

    def _make(
        opp_id: str | None = None,
        account_id: str = "001ACME000000000AAA",
        account_name: str = "Acme Restaurants",
        owner_id: str | None = "005OWNER00000000AAA",
        close_date: str = "2026-04-10",
        amount: float = 24000.0,
        name: str = "Acme — 2026 MSA",
    ) -> dict[str, Any]:
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


# ---------------------------------------------------------------- territory


@pytest.fixture
def tof_territory() -> dict[str, Any]:
    """A minimal territory.yaml-shaped dict used across ToF routing scenarios.

    Mirrors the real territory.yaml's dept_heads + segment rotations with
    deterministic single-slot rotations so round-robin assertions are stable.
    """
    return {
        "default_owner_id": "005FALLBACK",
        "summary_recipients": ["hutch@tryloop.ai"],
        "dept_heads": ["hutch@tryloop.ai", "charles@tryloop.ai"],
        "segments": {
            "ENT": {
                "min_locations": 50,
                "rotation": [
                    {"email": "charles@tryloop.ai", "slack_id": "U_CHARLES"},
                ],
            },
            "MM": {
                "min_locations": 10,
                "max_locations": 49,
                "rotation": [
                    {"email": "carlton@tryloop.ai", "slack_id": "U_CARL"},
                ],
            },
            "SMB": {
                "max_locations": 9,
                "rotation": [
                    {"email": "hutch@tryloop.ai", "slack_id": "U_HUTCH"},
                ],
            },
        },
    }
