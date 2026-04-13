"""Unit tests for schema.first_task_ceo_tier — the CEO canary orchestration."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support.schema import first_task_ceo_tier as canary
from shared.db.connection import get_engine
from shared.governance import ApprovalRequired, decide_approval_gate


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
    def __init__(self, roles: dict[str, int]):
        self.roles = roles

    def soql_query(self, q, limit=100):
        return {
            "records": [
                {"role_name": name, "cnt": cnt} for name, cnt in self.roles.items()
            ]
        }


def test_propose_writes_bundle_and_opens_gate():
    plan = canary.propose_ceo_role(justification="CEO above CRO for forecast fan-out")
    assert plan.slug == "canary-ceo-role"
    assert plan.gate_id > 0
    assert (plan.path / "force-app/main/default/roles/CEO.role-meta.xml").exists()
    assert (plan.path / "revert/destructiveChanges.xml").exists()

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["metadata_type"] == "Role"
    assert manifest["parent_role"] == "CRO"
    assert manifest["status"] == "proposed"


def test_propose_rejects_duplicate_bundle():
    canary.propose_ceo_role(justification="j")
    with pytest.raises(canary.CanaryError):
        canary.propose_ceo_role(justification="j")


def test_propose_requires_justification():
    with pytest.raises(ApprovalRequired):
        canary.propose_ceo_role(justification="")


def test_sandbox_happy_path_stamps_manifest():
    plan = canary.propose_ceo_role(justification="j")
    canary.sandbox_test(
        plan, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "s1"}
    )
    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["sandbox_test"]["status"] == "passed"
    assert manifest["status"] == "sandbox_passed"


def test_sandbox_failure_raises():
    plan = canary.propose_ceo_role(justification="j")
    with pytest.raises(canary.CanaryError):
        canary.sandbox_test(plan, deploy_fn=lambda *a, **k: {"success": False, "status": "Failed"})


def test_deploy_requires_sandbox_passed():
    plan = canary.propose_ceo_role(justification="j")
    canary.auto_approve_for_test(plan)
    sf = FakeSf({"CRO": 1, "VP Sales": 3})
    pre = canary.pre_snapshot(sf)
    with pytest.raises(canary.CanaryError):
        canary.deploy(plan, pre, sf_mcp=sf, deploy_fn=lambda *a, **k: {"success": True})


def test_deploy_requires_approved_gate():
    plan = canary.propose_ceo_role(justification="j")
    canary.sandbox_test(
        plan, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded"}
    )
    sf = FakeSf({"CRO": 1})
    pre = canary.pre_snapshot(sf)
    with pytest.raises(ApprovalRequired):
        canary.deploy(
            plan, pre, sf_mcp=sf,
            deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded"},
        )


def test_full_happy_path_records_pre_and_audit():
    plan = canary.propose_ceo_role(justification="canary")
    canary.sandbox_test(
        plan, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "s1"}
    )
    canary.auto_approve_for_test(plan)

    sf = FakeSf({"CRO": 1, "VP Sales": 3, "AE": 10})
    pre = canary.pre_snapshot(sf)
    canary.deploy(
        plan, pre, sf_mcp=sf,
        deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "prod-1"},
    )

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["status"] == "deployed"
    assert manifest["deploy_id"] == "prod-1"
    assert manifest["pre_snapshot"]["counts_by_role"]["CRO"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        audit = conn.execute(
            text("SELECT action, target FROM audit_log WHERE action = 'sf_schema_deploy'")
        ).fetchone()
    assert audit is not None
    assert audit[1] == "sf:UserRole:CEO"


def test_verify_passes_when_counts_match():
    plan = canary.propose_ceo_role(justification="j")
    sf = FakeSf({"CRO": 1, "VP Sales": 3})
    pre = canary.pre_snapshot(sf)

    # New CEO role appears post but shouldn't trip drift
    sf_post = FakeSf({"CEO": 1, "CRO": 1, "VP Sales": 3})
    result = canary.verify(plan, pre, interval_min=30, sf_mcp=sf_post)
    assert result.passed is True

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["verifications"][0]["interval_min"] == 30
    assert manifest["verifications"][0]["passed"] is True


def test_verify_fails_on_role_drift():
    plan = canary.propose_ceo_role(justification="j")
    sf = FakeSf({"CRO": 5, "VP Sales": 10})
    pre = canary.pre_snapshot(sf)

    # VP Sales lost a user post-deploy — drift!
    sf_post = FakeSf({"CEO": 1, "CRO": 5, "VP Sales": 8})
    result = canary.verify(plan, pre, interval_min=120, sf_mcp=sf_post)
    assert result.passed is False
    assert "VP Sales" in result.drift

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["verifications"][0]["passed"] is False


def test_verify_tolerance_absorbs_small_swings_on_large_buckets():
    plan = canary.propose_ceo_role(justification="j")
    # 10_000 AEs, -1 = 0.01% exactly — at tolerance boundary (should pass)
    sf = FakeSf({"AE": 10_000})
    pre = canary.pre_snapshot(sf)
    sf_post = FakeSf({"CEO": 1, "AE": 9_999})
    result = canary.verify(plan, pre, interval_min=30, sf_mcp=sf_post)
    assert result.passed is True

    # -2 = 0.02%, exceeds default 0.01% → drift
    sf_post2 = FakeSf({"CEO": 1, "AE": 9_998})
    result2 = canary.verify(plan, pre, interval_min=120, sf_mcp=sf_post2)
    assert result2.passed is False


def test_schedule_verifications_writes_cadence():
    plan = canary.propose_ceo_role(justification="j")
    t0 = datetime(2026, 4, 13, 14, 0, tzinfo=timezone.utc)
    scheduled = canary.schedule_verifications(plan, t0)
    assert [s["interval_min"] for s in scheduled] == [30, 120, 240]
    assert scheduled[0]["at"] == (t0 + timedelta(minutes=30)).isoformat()

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert len(manifest["scheduled_verifications"]) == 3


# ---------- Rollback ----------

def _deployed_plan(deploy_fn=None):
    """Fixture helper: propose → sandbox_test → approve → pre → deploy."""
    deploy_fn = deploy_fn or (
        lambda *a, **k: {"success": True, "status": "Succeeded", "id": "prod-1"}
    )
    plan = canary.propose_ceo_role(justification="j")
    canary.sandbox_test(
        plan, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "s1"}
    )
    canary.auto_approve_for_test(plan)
    sf = FakeSf({"CRO": 5, "AE": 10})
    pre = canary.pre_snapshot(sf)
    canary.deploy(plan, pre, sf_mcp=sf, deploy_fn=deploy_fn)
    return plan


def test_prepare_rollback_opens_delete_gate():
    plan = _deployed_plan()
    gate_id = canary.prepare_rollback(plan, justification="regret")
    assert gate_id > 0 and gate_id != plan.gate_id

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT action_type, status FROM approval_gates WHERE id = :i"),
            {"i": gate_id},
        ).fetchone()
    assert row[0] == "sf_schema_delete"
    assert row[1] == "pending"

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["rollback_gate_id"] == gate_id
    assert manifest["rollback_status"] == "pending"


def test_prepare_rollback_requires_justification():
    plan = _deployed_plan()
    with pytest.raises(ApprovalRequired):
        canary.prepare_rollback(plan, justification="")


def test_prepare_rollback_fails_without_revert_bundle():
    plan = _deployed_plan()
    # Delete the revert directory → should refuse to open a gate.
    import shutil
    shutil.rmtree(plan.path / "revert")
    with pytest.raises(canary.CanaryError):
        canary.prepare_rollback(plan, justification="j")


def _approve_delete_gate(primary_id: int) -> int:
    """Approve primary (→ approved_primary) then create + approve confirm child."""
    from shared.governance import create_approval_gate
    decide_approval_gate(primary_id, approved=True, approver="test")
    child_id = create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete_confirm",
        payload={"parent_gate_id": primary_id},
        justification="j",
    )
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE approval_gates SET parent_gate_id = :p WHERE id = :c"),
            {"p": primary_id, "c": child_id},
        )
    decide_approval_gate(child_id, approved=True, approver="test")
    return child_id


def test_execute_rollback_requires_prepared_gate():
    plan = _deployed_plan()
    with pytest.raises(canary.CanaryError):
        canary.execute_rollback(plan)


def test_execute_rollback_blocks_without_approved_confirm():
    plan = _deployed_plan()
    canary.prepare_rollback(plan, justification="j")
    # Primary exists but no confirm child → should refuse.
    with pytest.raises(ApprovalRequired):
        canary.execute_rollback(plan)


def test_execute_rollback_happy_path_audits_and_stamps():
    plan = _deployed_plan()
    rb_gate = canary.prepare_rollback(plan, justification="rollback")
    _approve_delete_gate(rb_gate)

    raw = canary.execute_rollback(
        plan,
        deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "rev-1"},
    )
    assert raw["success"] is True

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["rollback_status"] == "deployed"
    assert manifest["rollback_deploy_id"] == "rev-1"

    engine = get_engine()
    with engine.begin() as conn:
        audit = conn.execute(
            text("SELECT target, approval_gate_id FROM audit_log WHERE action = 'sf_schema_rollback'")
        ).fetchone()
    assert audit is not None
    assert audit[0] == "sf:UserRole:CEO"
    assert audit[1] == rb_gate


def test_execute_rollback_is_idempotent_after_success():
    """Re-running after a successful rollback should no-op, not re-deploy."""
    plan = _deployed_plan()
    rb_gate = canary.prepare_rollback(plan, justification="rollback")
    _approve_delete_gate(rb_gate)

    calls = []
    def track_deploy(*a, **k):
        calls.append((a, k))
        return {"success": True, "status": "Succeeded", "id": "rev-2"}

    canary.execute_rollback(plan, deploy_fn=track_deploy)
    assert len(calls) == 1

    # Second invocation: must no-op (no additional deploy call).
    result = canary.execute_rollback(plan, deploy_fn=track_deploy)
    assert len(calls) == 1
    assert result.get("no_op") is True


def test_execute_rollback_passes_destructive_changes_when_present():
    """Revert bundle has destructiveChangesPost.xml → deploy gets --manifest + --post-destructive-changes + --ignore-warnings."""
    plan = _deployed_plan()
    rb_gate = canary.prepare_rollback(plan, justification="rollback")
    _approve_delete_gate(rb_gate)

    captured = {}
    def capture(*args, **kwargs):
        captured.update(kwargs)
        return {"success": True, "status": "Succeeded", "id": "rev-3"}

    canary.execute_rollback(plan, deploy_fn=capture)
    assert captured.get("manifest", "").endswith("package.xml")
    assert captured.get("post_destructive_changes", "").endswith("destructiveChangesPost.xml")
    assert captured.get("ignore_warnings") is True
    assert captured.get("intent") == "write"


def test_execute_rollback_deploy_failure_stamps_manifest():
    plan = _deployed_plan()
    rb_gate = canary.prepare_rollback(plan, justification="rollback")
    _approve_delete_gate(rb_gate)

    def boom(*a, **k):
        raise RuntimeError("sf exploded")

    with pytest.raises(RuntimeError):
        canary.execute_rollback(plan, deploy_fn=boom)

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    assert manifest["rollback_status"] == "failed"
    assert "sf exploded" in manifest["rollback_error"]
