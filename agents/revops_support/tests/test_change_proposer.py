"""Unit tests for schema.change_proposer — covers happy paths + validation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support.schema import change_proposer as cp
from shared.db.connection import get_engine
from shared.governance import ApprovalRequired


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


def _create_intent() -> dict:
    return {
        "action": "create",
        "object": "Account",
        "field": {
            "name": "Churn_Risk__c",
            "type": "Number",
            "label": "Churn Risk",
            "precision": 3,
            "scale": 0,
            "description": "Rolling 30d churn risk score",
        },
    }


def test_create_field_writes_bundle_and_opens_gate():
    intent = _create_intent()
    result = cp.propose_change(intent, justification="CS agent needs risk surfacing")

    assert result.action == "create"
    assert result.action_type == "sf_schema_create"
    assert result.approval_gate_id > 0
    assert result.path.exists()

    field_xml = result.path / "force-app/main/default/objects/Account/fields/Churn_Risk__c.field-meta.xml"
    assert field_xml.exists()
    body = field_xml.read_text()
    assert "<fullName>Churn_Risk__c</fullName>" in body
    assert "<type>Number</type>" in body
    assert "<precision>3</precision>" in body

    pkg_xml = result.path / "force-app/main/default/package.xml"
    assert pkg_xml.exists()

    manifest = yaml.safe_load((result.path / "change.yaml").read_text())
    assert manifest["slug"] == result.slug
    assert manifest["action_type"] == "sf_schema_create"
    assert manifest["approval_gate_id"] == result.approval_gate_id
    assert manifest["status"] == "proposed"


def test_gate_row_has_action_and_justification():
    result = cp.propose_change(_create_intent(), justification="for churn")
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT action_type, justification, payload, status FROM approval_gates WHERE id = :i"),
            {"i": result.approval_gate_id},
        ).fetchone()
    assert row[0] == "sf_schema_create"
    assert row[1] == "for churn"
    assert row[3] == "pending"


def test_delete_action_writes_destructive_changes():
    intent = {
        "action": "delete",
        "object": "Account",
        "field": {"name": "Stale_Field__c"},
    }
    result = cp.propose_change(intent, justification="unused since 2024")
    assert result.action_type == "sf_schema_delete"

    destr = result.path / "force-app/main/default/destructiveChanges.xml"
    assert destr.exists()
    assert "Account.Stale_Field__c" in destr.read_text()
    assert "<name>CustomField</name>" in destr.read_text()

    # No field-meta.xml on delete
    assert not (result.path / "force-app/main/default/objects/Account/fields").exists()


def test_modify_action_uses_modify_tier():
    intent = _create_intent()
    intent["action"] = "modify"
    intent["field"]["description"] = "updated scope"
    result = cp.propose_change(intent, justification="broaden scope")
    assert result.action_type == "sf_schema_modify"


def test_missing_justification_rejected():
    with pytest.raises(ApprovalRequired):
        cp.propose_change(_create_intent(), justification="")
    with pytest.raises(ApprovalRequired):
        cp.propose_change(_create_intent(), justification="   ")


def test_invalid_action_rejected():
    intent = _create_intent()
    intent["action"] = "rename"
    with pytest.raises(cp.ChangeProposalError):
        cp.propose_change(intent, justification="x")


def test_invalid_field_name_rejected():
    intent = _create_intent()
    intent["field"]["name"] = "bad name"
    with pytest.raises(cp.ChangeProposalError):
        cp.propose_change(intent, justification="x")


def test_missing_type_on_create_rejected():
    intent = _create_intent()
    intent["field"].pop("type")
    with pytest.raises(cp.ChangeProposalError):
        cp.propose_change(intent, justification="x")


def test_load_proposal_round_trip():
    result = cp.propose_change(_create_intent(), justification="round trip")
    loaded = cp.load_proposal(result.slug)
    assert loaded["slug"] == result.slug
    assert loaded["object"] == "Account"
    assert loaded["field"]["name"] == "Churn_Risk__c"


def test_load_proposal_missing_raises():
    with pytest.raises(FileNotFoundError):
        cp.load_proposal("nope-does-not-exist")


def test_bundle_created_under_repo_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    _clear_state()
    result = cp.propose_change(_create_intent(), justification="root check")
    expected_parent = tmp_path / "agents" / "revops_support" / "pending_changes"
    assert result.path.parent == expected_parent


def test_two_proposals_same_field_get_distinct_slugs():
    from datetime import datetime, timezone, timedelta
    t1 = datetime(2026, 4, 13, 12, 0, 0, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=1)
    r1 = cp.propose_change(_create_intent(), justification="one", now=t1)
    r2 = cp.propose_change(_create_intent(), justification="two", now=t2)
    assert r1.slug != r2.slug
    assert r1.approval_gate_id != r2.approval_gate_id
