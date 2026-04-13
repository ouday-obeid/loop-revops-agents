"""Unit tests for schema.canary_poller — due-time + idempotency + drift alert."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support.schema import canary_poller as poller
from agents.revops_support.schema import first_task_ceo_tier as canary
from shared.db.connection import get_engine


def _clear_state() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log"))
        conn.execute(text("UPDATE approval_gates SET parent_gate_id = NULL"))
        conn.execute(text("DELETE FROM approval_gates"))
        conn.execute(text("DELETE FROM rate_limits"))


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    _clear_state()
    yield


class FakeSf:
    def __init__(self, roles):
        self.roles = roles

    def soql_query(self, q, limit=100):
        return {"records": [{"role_name": n, "cnt": c} for n, c in self.roles.items()]}


class FakeSlackSender:
    sent = []

    def send(self, *, channel, text_):
        FakeSlackSender.sent.append({"channel": channel, "text": text_})
        return "ts.123"


def _deployed_canary_with_schedule(deploy_time: datetime, pre_roles: dict[str, int]):
    plan = canary.propose_ceo_role(justification="j")
    canary.sandbox_test(
        plan, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded"}
    )
    canary.auto_approve_for_test(plan)
    sf = FakeSf(pre_roles)
    pre = canary.pre_snapshot(sf, now=deploy_time)
    canary.deploy(
        plan, pre, sf_mcp=sf,
        deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "prod-1"},
        now=deploy_time,
    )
    canary.schedule_verifications(plan, deploy_time)
    return plan


def test_poll_skips_verifications_not_yet_due():
    t0 = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    plan = _deployed_canary_with_schedule(t0, {"CRO": 1, "AE": 10})

    # Only 5 min after deploy; first check is at T+30m
    results = poller.poll(
        sf_mcp=FakeSf({"CEO": 1, "CRO": 1, "AE": 10}),
        now=t0 + timedelta(minutes=5),
    )
    assert results == []


def test_poll_fires_due_checks_and_marks_passed():
    t0 = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    plan = _deployed_canary_with_schedule(t0, {"CRO": 1, "AE": 10})

    # 35 min after deploy → T+30m check is due, T+2h/T+4h are not
    results = poller.poll(
        sf_mcp=FakeSf({"CEO": 1, "CRO": 1, "AE": 10}),
        now=t0 + timedelta(minutes=35),
    )
    assert len(results) == 1
    assert results[0]["interval_min"] == 30
    assert results[0]["passed"] is True


def test_poll_idempotent_does_not_refire_completed_check():
    t0 = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    plan = _deployed_canary_with_schedule(t0, {"CRO": 1, "AE": 10})

    # First poll at T+35m fires the 30m check
    poller.poll(sf_mcp=FakeSf({"CEO": 1, "CRO": 1, "AE": 10}), now=t0 + timedelta(minutes=35))
    # Second poll at T+45m should NOT refire the 30m check
    results = poller.poll(
        sf_mcp=FakeSf({"CEO": 1, "CRO": 1, "AE": 10}),
        now=t0 + timedelta(minutes=45),
    )
    assert results == []

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    # Only one verification recorded so far
    assert len(manifest["verifications"]) == 1


def test_poll_fires_multiple_due_checks_in_one_run():
    t0 = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    plan = _deployed_canary_with_schedule(t0, {"CRO": 1, "AE": 10})

    # 3 hours later → T+30m and T+2h are due; T+4h is not
    results = poller.poll(
        sf_mcp=FakeSf({"CEO": 1, "CRO": 1, "AE": 10}),
        now=t0 + timedelta(hours=3),
    )
    intervals = sorted(r["interval_min"] for r in results)
    assert intervals == [30, 120]


def test_poll_raises_drift_alert_to_slack():
    FakeSlackSender.sent = []
    t0 = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    plan = _deployed_canary_with_schedule(t0, {"CRO": 5, "AE": 10})

    # Post-deploy, AE lost 2 users → drift
    results = poller.poll(
        sf_mcp=FakeSf({"CEO": 1, "CRO": 5, "AE": 8}),
        now=t0 + timedelta(minutes=35),
    )
    assert len(results) == 1
    assert results[0]["passed"] is False

    # Drift alert goes through SlackSender — monkeypatch in a custom sender
    import agents.revops_support.schema.canary_poller as cp_module
    FakeSlackSender.sent = []
    cp_module._drift_alert(plan.slug, 30, {"AE": {"pre": 10, "post": 8}},
                           slack_sender_cls=FakeSlackSender)
    assert len(FakeSlackSender.sent) == 1
    assert "Canary drift" in FakeSlackSender.sent[0]["text"]
    assert plan.slug in FakeSlackSender.sent[0]["text"]


def test_poll_skips_non_deployed_bundles():
    # A proposed-but-not-deployed canary should not be polled.
    canary.propose_ceo_role(justification="j")
    results = poller.poll(sf_mcp=FakeSf({}), now=datetime(2026, 4, 13, 18, tzinfo=timezone.utc))
    assert results == []


def test_poll_handles_no_bundles_gracefully():
    assert poller.poll(sf_mcp=FakeSf({}), now=datetime.now(timezone.utc)) == []
