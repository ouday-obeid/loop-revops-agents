"""Tests for knowledge_refresh.scheduler + schedule.py registrations + cooldown_poller."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text


# ---------- schedule registry ----------

def test_schedule_registers_revops_support_jobs():
    from shared.runtime.schedule import SCHEDULE, by_name

    names = {j.name for j in SCHEDULE}
    assert "revops-support-metadata-refresh" in names
    assert "revops-support-metadata-digest" in names
    assert "revops-support-cooldown-poller" in names

    snap = by_name("revops-support-metadata-refresh")
    assert snap.cron == "0 2 * * 0"
    assert snap.callable_path.endswith(":run_weekly_snapshot")

    digest = by_name("revops-support-metadata-digest")
    assert digest.cron == "0 9 * * 1"
    assert digest.callable_path.endswith(":send_weekly_digest")

    poller = by_name("revops-support-cooldown-poller")
    assert poller.cron == "*/15 * * * *"
    assert poller.callable_path.endswith(":poll")


def test_launchd_renders_each_revops_job():
    from shared.runtime.launchd.generate import render
    from shared.runtime.schedule import by_name

    for name in (
        "revops-support-metadata-refresh",
        "revops-support-metadata-digest",
        "revops-support-cooldown-poller",
    ):
        job = by_name(name)
        xml = render(job, "/tmp/r", "/tmp/r/.venv/bin/python", "/tmp/r/var/log")
        assert "<key>Label</key>" in xml
        assert name in xml


# ---------- knowledge_refresh.scheduler ----------

def test_run_weekly_snapshot_produces_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.knowledge_refresh import scheduler, metadata_snapshotter as ms

    # Stub out SF entirely — empty data is fine for this structural test.
    monkeypatch.setattr(ms.salesforce_mcp, "_sf", lambda *a, **kw: {})
    monkeypatch.setattr(ms.salesforce_mcp, "describe_sobject", lambda n: {})
    monkeypatch.setattr(ms.salesforce_mcp, "tooling_query", lambda q: {"records": []})
    monkeypatch.setattr(ms.salesforce_mcp, "soql_query", lambda q, limit=100: {"records": []})

    paths = scheduler.run_weekly_snapshot()
    assert set(paths) == {"object_model", "automations", "users_roles"}
    for p in paths.values():
        assert p.exists()


def test_send_weekly_digest_no_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from agents.revops_support.knowledge_refresh import scheduler

    out = scheduler.send_weekly_digest()
    assert out["status"] == "no_snapshot"


def test_send_weekly_digest_sends_when_snapshot_present(monkeypatch, tmp_path):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("REVOPS_CANONICAL_KNOWLEDGE_DIR", str(tmp_path / "canon"))
    monkeypatch.setenv("REVOPS_KNOWLEDGE_DIGEST_CHANNEL", "oo-test-dm")

    from agents.revops_support.knowledge_refresh import scheduler, metadata_snapshotter as ms

    snap_dir = ms._snapshots_root() / "2026-04-20"
    snap_dir.mkdir(parents=True)
    (snap_dir / "sf_object_model.md").write_text("# SF\n## Account\nline A\n")
    (snap_dir / "sf_automations.md").write_text("# A\nflow\n")
    (snap_dir / "sf_users_roles.md").write_text("# U\nuser\n")

    canon = tmp_path / "canon"
    canon.mkdir()
    (canon / "sf_object_model.md").write_text("# SF\n## Account\nold line\n")
    (canon / "sf_automations.md").write_text("# A\nflow\n")
    (canon / "sf_users_roles.md").write_text("# U\nuser\n")

    sent: dict[str, object] = {}

    class FakeSender:
        def __init__(self):
            pass

        def send(self, channel, text_=None, **kw):
            sent["channel"] = channel
            sent["text"] = text_
            return "ts-1"

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", FakeSender)

    out = scheduler.send_weekly_digest()
    assert out["status"] == "sent"
    assert out["message_ts"] == "ts-1"
    assert sent["channel"] == "oo-test-dm"
    assert "Knowledge Refresh" in sent["text"]
    assert "## sf_object_model.md" in sent["text"]


# ---------- schema.cooldown_poller ----------

@pytest.fixture
def _clean_gates():
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM approval_gates"))
    yield
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM approval_gates"))


def _insert_primary(now: datetime, cooldown_offset_hours: float, *, action_type="sf_schema_delete"):
    from shared.db.connection import get_engine
    cd = now + timedelta(hours=cooldown_offset_hours)
    payload = '{"target": "Opportunity.Legacy_Field__c"}'
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, cooldown_until) "
                "VALUES ('revops_support', :act, :pl, 'unused since 2024-06', 'O', "
                " 'approved_primary', :rq, :cd)"
            ),
            {"act": action_type, "pl": payload, "rq": now, "cd": cd},
        )
        gate_id = result.lastrowid
        if gate_id is None:
            gate_id = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()[0]
        return int(gate_id)


def test_cooldown_poller_elevates_ready_primary(monkeypatch, _clean_gates):
    from agents.revops_support.schema import cooldown_poller

    class StubSender:
        sent: list[dict] = []

        def __init__(self):
            pass

        def send(self, channel, text_=None, **kw):
            StubSender.sent.append({"channel": channel, "text": text_})
            return "ts-99"

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", StubSender)

    now = datetime.now(timezone.utc)
    parent_id = _insert_primary(now, cooldown_offset_hours=-1)

    out = cooldown_poller.poll()
    assert len(out) == 1
    assert out[0]["parent_id"] == parent_id
    assert out[0]["child_id"] and out[0]["child_id"] != parent_id
    assert out[0]["slack_ts"] == "ts-99"

    # Second poll must not double-elevate.
    out2 = cooldown_poller.poll()
    assert out2 == []

    # Slack was posted exactly once with expected context.
    assert len(StubSender.sent) == 1
    assert "Opportunity.Legacy_Field__c" in StubSender.sent[0]["text"]


def test_cooldown_poller_skips_unexpired(monkeypatch, _clean_gates):
    from agents.revops_support.schema import cooldown_poller

    class StubSender:
        def __init__(self):
            pass

        def send(self, channel, text_=None, **kw):
            return "ignored"

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", StubSender)

    now = datetime.now(timezone.utc)
    _insert_primary(now, cooldown_offset_hours=2)  # cooldown in the future

    out = cooldown_poller.poll()
    assert out == []


def test_cooldown_poller_handles_slack_failure(monkeypatch, _clean_gates):
    """Slack send failure must not abort the elevation — gate still created."""
    from agents.revops_support.schema import cooldown_poller

    class BrokenSender:
        def __init__(self):
            pass

        def send(self, *a, **kw):
            raise RuntimeError("slack down")

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", BrokenSender)

    now = datetime.now(timezone.utc)
    parent_id = _insert_primary(now, cooldown_offset_hours=-1)
    out = cooldown_poller.poll()
    assert len(out) == 1
    assert out[0]["parent_id"] == parent_id
    assert out[0]["slack_ts"] is None

    # Child gate exists with correct action_type and parent_gate_id linkage.
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT action_type, parent_gate_id, status FROM approval_gates "
                "WHERE id = :i"
            ),
            {"i": out[0]["child_id"]},
        ).fetchone()
    assert row[0] == "sf_schema_delete_confirm"
    assert row[1] == parent_id
    assert row[2] == "pending"
