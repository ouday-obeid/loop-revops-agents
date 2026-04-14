"""Loop Pulse BigQuery client — degradation, retry, health writes."""
from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import text

from agents.slt_metrics.bigquery.loop_pulse_client import (
    BigQueryUnavailable,
    LoopPulseClient,
)
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _clear_bq_env(monkeypatch):
    # Integration_health entries live in the same DB; wipe between tests.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM integration_health WHERE integration = 'slt_loop_pulse'"))
    monkeypatch.delenv("BQ_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("BQ_PROJECT", raising=False)
    yield


class _FakeClient:
    """Test double — mimics the `.execute_query(sql, params)` contract."""

    def __init__(self, *, rows: list[dict[str, Any]] | None = None, raises: Exception | None = None):
        self.rows = rows or []
        self.raises = raises
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute_query(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if self.raises:
            raise self.raises
        return self.rows


def _count_health_rows(status: str | None = None) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        if status:
            return conn.execute(
                text(
                    "SELECT COUNT(*) FROM integration_health "
                    "WHERE integration = 'slt_loop_pulse' AND status = :s"
                ),
                {"s": status},
            ).scalar() or 0
        return conn.execute(
            text("SELECT COUNT(*) FROM integration_health WHERE integration = 'slt_loop_pulse'"),
        ).scalar() or 0


# ------------------------------------------------------------------ is_connected

def test_is_connected_false_without_creds():
    client = LoopPulseClient(creds_json=None, project=None)
    assert client.is_connected() is False


def test_is_connected_false_with_placeholder_creds():
    client = LoopPulseClient(creds_json="REPLACE", project="foo")
    assert client.is_connected() is False


def test_is_connected_true_with_injected_factory():
    fake = _FakeClient()
    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x",
        client_factory=lambda **kwargs: fake,
    )
    assert client.is_connected() is True


def test_is_connected_caches_probe_result():
    calls = {"n": 0}

    def factory(**_):
        calls["n"] += 1
        return _FakeClient()

    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x", client_factory=factory,
    )
    client.is_connected()
    client.is_connected()
    assert calls["n"] == 1   # cached after first probe


# ------------------------------------------------------------------ query success

def test_query_returns_rows_and_writes_healthy_row():
    fake = _FakeClient(rows=[{"metric": "nrr", "value": 1.12}])
    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x",
        client_factory=lambda **kwargs: fake,
    )
    rows = client.query("SELECT 1", {"run_date": "2026-04-13"})
    assert rows == [{"metric": "nrr", "value": 1.12}]
    assert len(fake.calls) == 1
    assert _count_health_rows("healthy") >= 1


def test_query_not_connected_raises_sentinel():
    client = LoopPulseClient(creds_json=None, project=None)
    with pytest.raises(BigQueryUnavailable):
        client.query("SELECT 1")
    assert _count_health_rows("down") >= 1


def test_query_without_params_is_valid():
    fake = _FakeClient(rows=[{"x": 1}])
    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x",
        client_factory=lambda **kwargs: fake,
    )
    rows = client.query("SELECT 1")
    assert rows == [{"x": 1}]


# ------------------------------------------------------------------ retry behavior

def test_query_retries_and_raises_on_persistent_failure(monkeypatch):
    fake = _FakeClient(raises=RuntimeError("BQ timeout"))
    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x",
        client_factory=lambda **kwargs: fake,
    )
    # Skip actual backoff sleeps for test speed.
    import agents.slt_metrics.bigquery.loop_pulse_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    with pytest.raises(BigQueryUnavailable, match="Loop Pulse query failed"):
        client.query("SELECT 1")
    # Three attempts = three sql invocations.
    assert len(fake.calls) == 3
    # Final status row should be 'down'.
    assert _count_health_rows("down") >= 1


def test_query_recovers_on_second_attempt(monkeypatch):
    class _FlakyClient:
        def __init__(self):
            self.calls = 0

        def execute_query(self, sql, params):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return [{"ok": True}]

    flaky = _FlakyClient()
    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x",
        client_factory=lambda **kwargs: flaky,
    )
    import agents.slt_metrics.bigquery.loop_pulse_client as mod
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    rows = client.query("SELECT 1")
    assert rows == [{"ok": True}]
    # healthy row written after recovery.
    assert _count_health_rows("healthy") >= 1


# ------------------------------------------------------------------ missing deps

def test_query_raises_when_google_cloud_not_installed(monkeypatch):
    """No factory + no google-cloud-bigquery installed → BigQueryUnavailable."""
    # Force the lazy import to fail by stubbing the attr directly.
    import sys
    # Remove any cached google.cloud.bigquery (even the stub) to force reload.
    for mod_name in [m for m in list(sys.modules) if m.startswith("google.cloud")]:
        sys.modules.pop(mod_name, None)
    # Block the import during this test.
    import builtins
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name.startswith("google.cloud") or name == "google":
            raise ImportError("google-cloud-bigquery not installed in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    client = LoopPulseClient(creds_json='{"project_id": "x"}', project="x")
    # is_connected itself tries to import and caches False.
    assert client.is_connected() is False
    with pytest.raises(BigQueryUnavailable):
        client.query("SELECT 1")


# ------------------------------------------------------------------ async shim

@pytest.mark.asyncio
async def test_query_async_wraps_sync_query():
    fake = _FakeClient(rows=[{"metric": "arr", "value": 14_000_000}])
    client = LoopPulseClient(
        creds_json='{"project_id": "x"}', project="x",
        client_factory=lambda **kwargs: fake,
    )
    rows = await client.query_async("SELECT 1")
    assert rows[0]["value"] == 14_000_000
