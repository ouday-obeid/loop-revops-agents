"""Unit tests for schema.sandbox_tester — stub the SF deploy call."""
from __future__ import annotations

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support.schema import change_proposer as cp
from agents.revops_support.schema import sandbox_tester as st
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


def _propose() -> cp.ProposedChange:
    intent = {
        "action": "create",
        "object": "Account",
        "field": {
            "name": "Churn_Risk__c",
            "type": "Number",
            "label": "Churn Risk",
            "precision": 3,
            "scale": 0,
        },
    }
    return cp.propose_change(intent, justification="CS agent surface")


def test_sandbox_pass_stamps_manifest():
    change = _propose()
    calls = []

    def fake_deploy(source_dir, *, intent, check_only, test_level):
        calls.append({"source": source_dir, "intent": intent, "test_level": test_level})
        return {"success": True, "status": "Succeeded", "id": "0Af000001"}

    result = st.test(change.slug, deploy_fn=fake_deploy)
    assert result.status == "passed"
    assert result.deploy_id == "0Af000001"
    assert len(calls) == 1
    assert calls[0]["intent"] == "sandbox"
    assert calls[0]["test_level"] == "RunLocalTests"
    assert "force-app" in calls[0]["source"]

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["status"] == "sandbox_passed"
    assert manifest["sandbox_test"]["status"] == "passed"
    assert manifest["sandbox_test"]["deploy_id"] == "0Af000001"


def test_sandbox_fail_captures_component_failures():
    change = _propose()

    def fake_deploy(source_dir, *, intent, check_only, test_level):
        return {
            "success": False,
            "status": "Failed",
            "id": "0Af000002",
            "details": {
                "componentFailures": [
                    {"componentType": "CustomField", "problem": "duplicate field"},
                ],
                "runTestResult": {"failures": []},
            },
        }

    result = st.test(change.slug, deploy_fn=fake_deploy)
    assert result.status == "failed"
    assert len(result.component_failures) == 1
    assert result.component_failures[0]["problem"] == "duplicate field"

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["status"] == "sandbox_failed"
    assert manifest["sandbox_test"]["component_failures"][0]["problem"] == "duplicate field"


def test_sandbox_apex_test_failures_mark_failed():
    change = _propose()

    def fake_deploy(source_dir, *, intent, check_only, test_level):
        return {
            "success": False,
            "status": "Failed",
            "details": {
                "componentFailures": [],
                "runTestResult": {
                    "failures": [
                        {"name": "AccountTriggerTest.testChurnRisk", "message": "boom"},
                    ],
                },
            },
        }

    result = st.test(change.slug, deploy_fn=fake_deploy)
    assert result.status == "failed"
    assert len(result.test_failures) == 1
    assert result.test_failures[0]["name"] == "AccountTriggerTest.testChurnRisk"


def test_sandbox_exception_captured_as_error():
    change = _propose()

    def fake_deploy(source_dir, *, intent, check_only, test_level):
        raise RuntimeError("sandbox alias missing")

    result = st.test(change.slug, deploy_fn=fake_deploy)
    assert result.status == "error"
    assert "sandbox alias missing" in result.error_message

    manifest = yaml.safe_load((change.path / "change.yaml").read_text())
    assert manifest["status"] == "sandbox_failed"
    assert "sandbox alias missing" in manifest["sandbox_test"]["error_message"]


def test_sandbox_missing_bundle_raises():
    with pytest.raises(FileNotFoundError):
        st.test("does-not-exist", deploy_fn=lambda *a, **k: {})


def test_dict_single_failure_normalized_to_list():
    change = _propose()

    def fake_deploy(source_dir, *, intent, check_only, test_level):
        return {
            "success": False,
            "status": "Failed",
            "details": {
                "componentFailures": {"problem": "one failure returned as dict"},
                "runTestResult": {"failures": {"name": "SoloTest", "message": "x"}},
            },
        }

    result = st.test(change.slug, deploy_fn=fake_deploy)
    assert result.status == "failed"
    assert len(result.component_failures) == 1
    assert len(result.test_failures) == 1
