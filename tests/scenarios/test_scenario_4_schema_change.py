"""Scenario 4 — SF schema change via Slack (RevOps Support).

Monday item: 11736870029
Path: `@oo revops-support schema propose ...` (future Slack surface) →
`schema.change_proposer.propose_change` → approval gate (pending) + bundle
written + manifest.

What we validate (Phase 1 DoD: governance trail for schema mutation):

  1. RevOps Support dispatcher currently returns a "ships later" hint for
     `schema ...` (it is an explicit entry in _FUTURE_COMMANDS). We assert
     that routing decision so a rename doesn't silently break the scenario.
  2. The backing `change_proposer.propose_change` call — which the Slack path
     will invoke once wired — produces a pending `sf_schema_create` gate
     with justification required.
  3. Bundle artifacts (field-meta.xml + package.xml + change.yaml) exist on
     disk under REVOPS_REPO_ROOT/agents/revops_support/pending_changes/<slug>/.
  4. Rate limit bucket `revops_schema_changes_weekly` was incremented (soft
     limit — does not raise).

Shortcut: we do not exercise sandbox_tester / metadata_deployer / canary_poller
here — each has its own dedicated test suite. The scenario boundary for
"Slack-invoked schema change" is the proposal + governance surface.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from agents.revops_support import agent as rs_agent
from agents.revops_support.schema import change_proposer
from shared.db.connection import get_engine
from shared.governance import ApprovalRequired


@pytest.fixture
def pending_root(monkeypatch, tmp_path):
    """Isolated repo root so the scenario doesn't collide with other tests'
    pending_changes/ directories.
    """
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    yield tmp_path


@pytest.fixture(autouse=True)
def _clean():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM approval_gates WHERE agent_name = 'revops_support'"
        ))
        conn.execute(text(
            "DELETE FROM rate_limits WHERE bucket = 'revops_schema_changes_weekly'"
        ))
    yield


@pytest.mark.asyncio
async def test_dispatcher_defers_schema_command_today():
    """Slack `@oo revops-support schema ...` returns the 'ships later' hint.

    When the schema-propose Slack surface lands in a later week, this test is
    the canary: update the expected text here and add a positive assertion.
    """
    agent = rs_agent.RevOpsSupportAgent()
    result = await agent.handle(
        trigger="slack",
        payload={"text": "schema propose Account Churn_Risk__c Number"},
    )
    text_out = result["text"].lower()
    assert "ships later" in text_out or "not yet wired" in text_out
    assert "schema" in text_out


def test_propose_change_opens_pending_gate_and_writes_bundle(pending_root):
    intent = {
        "action": "create",
        "object": "Account",
        "field": {
            "name": "Churn_Risk__c",
            "type": "Number",
            "label": "Churn Risk",
            "precision": 3,
            "scale": 0,
            "description": "Rolling 30d churn risk score (seed by CS agent)",
        },
    }

    result = change_proposer.propose_change(
        intent,
        justification="CS agent wants a native field instead of Description blob",
        requested_by="user:ouday",
    )

    # Boundary 1 — pending gate with the right tier + payload.
    engine = get_engine()
    with engine.begin() as conn:
        gate = conn.execute(
            text(
                "SELECT agent_name, action_type, status, justification, requested_by "
                "FROM approval_gates WHERE id = :i"
            ),
            {"i": result.approval_gate_id},
        ).mappings().first()
    assert gate is not None
    assert gate["agent_name"] == "revops_support"
    assert gate["action_type"] == "sf_schema_create"
    assert gate["status"] == "pending"
    assert gate["requested_by"] == "user:ouday"
    assert "CS agent" in gate["justification"]

    # Boundary 2 — bundle files exist under REVOPS_REPO_ROOT.
    assert result.path.is_dir()
    field_xml = (
        result.path
        / "force-app/main/default/objects/Account/fields/Churn_Risk__c.field-meta.xml"
    )
    assert field_xml.exists()
    body = field_xml.read_text()
    assert "<fullName>Churn_Risk__c</fullName>" in body
    assert "<type>Number</type>" in body

    package_xml = result.path / "force-app/main/default/package.xml"
    assert package_xml.exists()
    manifest = yaml.safe_load((result.path / "change.yaml").read_text())
    assert manifest["slug"] == result.slug
    assert manifest["action_type"] == "sf_schema_create"
    assert manifest["approval_gate_id"] == result.approval_gate_id

    # Boundary 3 — weekly rate-limit bucket got a row for this window.
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT count FROM rate_limits WHERE bucket = :b "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"b": "revops_schema_changes_weekly"},
        ).fetchone()
    assert row is not None
    assert int(row[0]) >= 1


def test_propose_change_requires_justification(pending_root):
    """No justification → ApprovalRequired before any file is written."""
    intent = {
        "action": "create",
        "object": "Account",
        "field": {
            "name": "Churn_Risk__c",
            "type": "Number",
            "label": "Churn Risk",
        },
    }
    with pytest.raises(ApprovalRequired, match="justification"):
        change_proposer.propose_change(intent, justification="")
