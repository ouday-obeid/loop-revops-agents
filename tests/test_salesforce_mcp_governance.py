"""Critical: bulk_update ≥100 must reject BEFORE any sf CLI invocation."""
from unittest.mock import patch

import pytest

from shared.mcp import salesforce_mcp
from shared.governance import ApprovalRequired


def test_bulk_update_500_without_gate_rejects_before_sf():
    updates = [{"Id": f"00{i:010d}", "Foo__c": "bar"} for i in range(500)]
    with patch.object(salesforce_mcp, "_sf") as mock_sf:
        with pytest.raises(ApprovalRequired):
            salesforce_mcp.bulk_update("Account", updates, agent_name="test", approval_gate_id=None)
    mock_sf.assert_not_called()


def test_bulk_update_500_with_pending_gate_rejects():
    from shared import governance
    gid = governance.create_approval_gate(
        agent_name="test", action_type="bulk_update_large",
        payload={"count": 500}, justification="test"
    )
    updates = [{"Id": f"00{i:010d}"} for i in range(500)]
    with patch.object(salesforce_mcp, "_sf") as mock_sf:
        with pytest.raises(ApprovalRequired):
            salesforce_mcp.bulk_update("Account", updates, agent_name="test", approval_gate_id=gid)
    mock_sf.assert_not_called()


def test_bulk_update_500_with_approved_gate_proceeds():
    from shared import governance
    gid = governance.create_approval_gate(
        agent_name="test", action_type="bulk_update_large",
        payload={"count": 500}, justification="test"
    )
    governance.decide_approval_gate(gid, approved=True, approver="UTEST")
    updates = [{"Id": f"00{i:010d}"} for i in range(500)]
    result = salesforce_mcp.bulk_update("Account", updates, agent_name="test", approval_gate_id=gid)
    assert result["simulated"] is True
    assert result["count"] == 500


def test_intent_routing_resolves_correct_alias(monkeypatch):
    monkeypatch.setenv("SF_ORG_ALIAS", "salesops")
    monkeypatch.setenv("SF_WRITE_ORG_ALIAS", "revops-agent-prod")
    monkeypatch.setenv("SF_SANDBOX_ORG_ALIAS", "salesops-sandbox")
    assert salesforce_mcp._resolve_org_alias("read") == "salesops"
    assert salesforce_mcp._resolve_org_alias("write") == "revops-agent-prod"
    assert salesforce_mcp._resolve_org_alias("sandbox") == "salesops-sandbox"


def test_write_intent_falls_back_to_read_alias(monkeypatch):
    monkeypatch.setenv("SF_ORG_ALIAS", "salesops")
    monkeypatch.delenv("SF_WRITE_ORG_ALIAS", raising=False)
    assert salesforce_mcp._resolve_org_alias("write") == "salesops"


def test_sandbox_intent_requires_explicit_alias(monkeypatch):
    monkeypatch.delenv("SF_SANDBOX_ORG_ALIAS", raising=False)
    with pytest.raises(salesforce_mcp.SalesforceError):
        salesforce_mcp._resolve_org_alias("sandbox")
