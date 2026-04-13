"""Unit tests for schema.metadata_deployer — prod deploy with pre-snapshot."""
from __future__ import annotations

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support.schema import change_proposer as cp
from agents.revops_support.schema import metadata_deployer as md
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


def _propose_create() -> cp.ProposedChange:
    return cp.propose_change(
        {
            "action": "create",
            "object": "Account",
            "field": {"name": "Churn_Risk__c", "type": "Number", "label": "Churn Risk"},
        },
        justification="CS agent needs churn signal",
    )


def _pass_sandbox(change: cp.ProposedChange) -> None:
    st.test(change.slug, deploy_fn=lambda *a, **k: {"success": True, "status": "Succeeded"})


def _ok_deploy(*a, **k):
    return {"success": True, "status": "Succeeded", "id": "0Af_PROD"}


def test_deploy_happy_path_create():
    change = _propose_create()
    _pass_sandbox(change)
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")

    result = md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})
    assert result.success is True
    assert result.deploy_id == "0Af_PROD"
    assert result.audit_id is not None

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["status"] == "deployed"
    assert manifest["deploy_id"] == "0Af_PROD"

    # Revert bundle written for create (destructive)
    assert (change.path / "revert" / "destructiveChanges.xml").exists()


def test_deploy_rejected_without_approved_gate():
    change = _propose_create()
    _pass_sandbox(change)
    # Gate is still pending

    with pytest.raises(ApprovalRequired):
        md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})


def test_deploy_rejected_if_sandbox_not_passed():
    change = _propose_create()
    # Sandbox explicitly fails
    st.test(change.slug, deploy_fn=lambda *a, **k: {"success": False, "status": "Failed"})
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")

    with pytest.raises(md.DeployPreconditionError):
        md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})


def test_deploy_rejected_if_no_sandbox_test():
    change = _propose_create()
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")
    with pytest.raises(md.DeployPreconditionError):
        md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})


def test_modify_retrieves_pre_snapshot():
    change = cp.propose_change(
        {
            "action": "modify",
            "object": "Account",
            "field": {
                "name": "Churn_Risk__c",
                "type": "Number",
                "label": "Churn Risk v2",
                "description": "new scope",
            },
        },
        justification="broaden",
    )
    _pass_sandbox(change)
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")

    retrieve_calls = []

    def fake_retrieve(metadata_item, *, target_dir, intent):
        retrieve_calls.append({"metadata": metadata_item, "target_dir": target_dir})
        return {}

    result = md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=fake_retrieve)
    assert result.success is True
    assert len(retrieve_calls) == 1
    assert retrieve_calls[0]["metadata"] == "CustomField:Account.Churn_Risk__c"
    # Modify revert package is a re-deploy manifest (no destructiveChanges)
    assert (change.path / "revert" / "package.xml").exists()
    assert not (change.path / "revert" / "destructiveChanges.xml").exists()


def test_delete_requires_confirmed_child_gate():
    change = cp.propose_change(
        {"action": "delete", "object": "Account", "field": {"name": "Stale__c"}},
        justification="unused since 2024",
    )
    _pass_sandbox(change)
    # Primary approved (enters 24h cooldown)
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")

    # No confirmation child yet → rejected
    with pytest.raises(ApprovalRequired):
        md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})

    # Simulate cooldown poller: create + approve child
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

    result = md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})
    assert result.success is True


def test_deploy_failure_stamps_manifest_and_returns_revert_dir():
    change = _propose_create()
    _pass_sandbox(change)
    decide_approval_gate(change.approval_gate_id, approved=True, approver="o")

    def bad_deploy(*a, **k):
        raise RuntimeError("quota exceeded")

    result = md.deploy(change.slug, deploy_fn=bad_deploy, retrieve_fn=lambda *a, **k: {})
    assert result.success is False
    assert "quota exceeded" in result.error_message
    assert result.revert_dir is not None

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["status"] == "deploy_failed"
    assert "quota exceeded" in manifest["deploy_error"]


def test_rate_limit_daily_cap():
    from shared.governance import RateLimitExceeded

    # Fill the daily bucket (5/day for revops_metadata_deploy_daily).
    from datetime import datetime, timezone, timedelta
    for i in range(5):
        change = cp.propose_change(
            {
                "action": "create",
                "object": "Account",
                "field": {"name": f"Field{i}__c", "type": "Text", "label": f"F{i}", "length": 10},
            },
            justification=f"reason {i}",
            now=datetime.now(timezone.utc) + timedelta(seconds=i),
        )
        _pass_sandbox(change)
        decide_approval_gate(change.approval_gate_id, approved=True, approver="o")
        md.deploy(change.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})

    # 6th deploy in same day → over cap
    change6 = cp.propose_change(
        {
            "action": "create",
            "object": "Account",
            "field": {"name": "Sixth__c", "type": "Text", "label": "sixth", "length": 5},
        },
        justification="one too many",
        now=datetime.now(timezone.utc) + timedelta(seconds=10),
    )
    _pass_sandbox(change6)
    decide_approval_gate(change6.approval_gate_id, approved=True, approver="o")
    with pytest.raises(RateLimitExceeded):
        md.deploy(change6.slug, deploy_fn=_ok_deploy, retrieve_fn=lambda *a, **k: {})
