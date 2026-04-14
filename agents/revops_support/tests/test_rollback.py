"""Unit tests for schema.rollback — prepare gate + execute revert."""
from __future__ import annotations

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support.schema import change_proposer as cp
from agents.revops_support.schema import metadata_deployer as md
from agents.revops_support.schema import rollback as rb
from agents.revops_support.schema import sandbox_tester as st
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


def _deploy_and_return(action: str = "create") -> cp.ProposedChange:
    intent = {
        "action": action,
        "object": "Account",
        "field": {"name": "Churn_Risk__c", "type": "Number", "label": "Churn Risk"},
    }
    if action == "modify":
        intent["field"]["description"] = "new scope"
    change = cp.propose_change(intent, justification="reason")
    st.test(change.slug, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded"})
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")
    md.deploy(
        change.slug,
        deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "prod-id"},
        retrieve_fn=lambda *a, **k: {},
    )
    return change


def test_prepare_opens_rollback_gate_and_stamps_manifest():
    change = _deploy_and_return("create")
    gate_id = rb.prepare(change.slug, justification="prod incident at 14:02")
    assert gate_id > 0

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT action_type, status FROM approval_gates WHERE id = :i"),
            {"i": gate_id},
        ).fetchone()
    # Rollback of a create = destructive delete
    assert row[0] == "sf_schema_delete"
    assert row[1] == "pending"

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["rollback_gate_id"] == gate_id
    assert manifest["rollback_status"] == "pending"


def test_prepare_requires_justification():
    change = _deploy_and_return("create")
    with pytest.raises(ApprovalRequired):
        rb.prepare(change.slug, justification="")


def test_execute_rejected_without_approval():
    change = _deploy_and_return("create")
    rb.prepare(change.slug, justification="rollback needed")
    with pytest.raises(ApprovalRequired):
        rb.execute(change.slug, deploy_fn=lambda *a, **k: {"success": True})


def test_execute_happy_path_modify_rollback():
    change = _deploy_and_return("modify")
    gate_id = rb.prepare(change.slug, justification="modify broke a flow")
    decide_approval_gate(gate_id, approved=True, approver="o")

    calls = []

    def fake_deploy(source_dir, *, intent, check_only):
        calls.append({"source": source_dir, "intent": intent})
        return {"success": True, "status": "Succeeded", "id": "revert-id"}

    result = rb.execute(change.slug, deploy_fn=fake_deploy)
    assert result.success is True
    assert result.deploy_id == "revert-id"
    assert "revert" in calls[0]["source"]
    assert calls[0]["intent"] == "write"

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["rollback_status"] == "deployed"
    assert manifest["rollback_deploy_id"] == "revert-id"
    # Top-level status must flip so canary_poller stops firing verify on the
    # reverted bundle.
    assert manifest["status"] == "rolled_back"


def test_execute_idempotent_noop_if_already_rolled_back():
    change = _deploy_and_return("modify")
    gate_id = rb.prepare(change.slug, justification="incident")
    decide_approval_gate(gate_id, approved=True, approver="o")

    def fake_deploy(*a, **k):
        return {"success": True, "status": "Succeeded", "id": "revert-1"}

    rb.execute(change.slug, deploy_fn=fake_deploy)

    # Second call — the stamped status should short-circuit without deploying again.
    def should_not_run(*a, **k):
        raise AssertionError("deploy_fn should not be called on idempotent re-run")

    result2 = rb.execute(change.slug, deploy_fn=should_not_run)
    assert result2.success is True
    assert result2.deploy_id == "revert-1"


def test_delete_rollback_not_supported_v1():
    # Deleting + rolling back a delete = re-provisioning. Flagged for manual
    # handling in v1 to avoid silently failing deploys when the snapshot does
    # not include the data payload.
    change = cp.propose_change(
        {"action": "delete", "object": "Account", "field": {"name": "Stale__c"}},
        justification="unused",
    )
    st.test(change.slug, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded"})
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")

    from shared.governance import create_approval_gate
    child_id = create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete_confirm",
        payload={"parent_gate_id": change.approval_gate_id},
        justification=None,
        requested_by="cooldown_poller",
    )
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE approval_gates SET parent_gate_id = :p WHERE id = :c"),
            {"p": change.approval_gate_id, "c": child_id},
        )
    decide_approval_gate(child_id, approved=True, approver="o")
    md.deploy(
        change.slug,
        deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded", "id": "d"},
        retrieve_fn=lambda *a, **k: {},
    )

    rb_gate = rb.prepare(change.slug, justification="restore needed")
    decide_approval_gate(rb_gate, approved=True, approver="o")
    with pytest.raises(rb.RollbackError):
        rb.execute(change.slug, deploy_fn=lambda *a, **k: {"success": True})


def test_execute_deploy_failure_stamps_manifest():
    change = _deploy_and_return("modify")
    gate_id = rb.prepare(change.slug, justification="incident")
    decide_approval_gate(gate_id, approved=True, approver="o")

    def bad_deploy(*a, **k):
        raise RuntimeError("network down")

    result = rb.execute(change.slug, deploy_fn=bad_deploy)
    assert result.success is False
    assert "network down" in result.error_message

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["rollback_status"] == "failed"
    assert "network down" in manifest["rollback_error"]
    # Failed rollback leaves the original status intact so operators can retry.
    assert manifest["status"] != "rolled_back"
