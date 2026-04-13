"""Exercise individual _check_* paths in integration_health."""
import asyncio
from unittest.mock import MagicMock, patch


def test_check_salesforce_healthy():
    from agents.oo import integration_health
    from shared.mcp import salesforce_mcp
    with patch.object(salesforce_mcp, "soql_query", return_value={"totalSize": 42, "records": []}):
        status, err = asyncio.run(integration_health._check_salesforce())
    assert status == "healthy"
    assert err is None


def test_check_salesforce_zero_users_is_degraded():
    from agents.oo import integration_health
    from shared.mcp import salesforce_mcp
    with patch.object(salesforce_mcp, "soql_query", return_value={"totalSize": 0, "records": []}):
        status, _ = asyncio.run(integration_health._check_salesforce())
    assert status == "degraded"


def test_check_salesforce_down_on_error():
    from agents.oo import integration_health
    from shared.mcp import salesforce_mcp
    with patch.object(salesforce_mcp, "soql_query", side_effect=RuntimeError("auth fail")):
        status, err = asyncio.run(integration_health._check_salesforce())
    assert status == "down"
    assert "auth fail" in err


def test_check_fireflies_no_key(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("FIREFLIES_API_KEY", "REPLACE")
    status, err = asyncio.run(integration_health._check_fireflies())
    assert status == "degraded"


def test_check_fireflies_healthy(monkeypatch):
    from agents.oo import integration_health
    from shared.mcp import fireflies_mcp
    monkeypatch.setenv("FIREFLIES_API_KEY", "fake-key")
    with patch.object(fireflies_mcp, "list_transcripts", return_value=[{"id": "x"}]):
        status, _ = asyncio.run(integration_health._check_fireflies())
    assert status == "healthy"


def test_check_slack_no_token(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("SLACK_BOT_TOKEN", "REPLACE")
    status, _ = asyncio.run(integration_health._check_slack(None))
    assert status in ("degraded", "down")


def test_check_slack_with_mock_client():
    from agents.oo import integration_health
    client = MagicMock()
    client.auth_test.return_value = {"ok": True}
    status, _ = asyncio.run(integration_health._check_slack(client))
    assert status == "healthy"


def test_check_vitally_no_key(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("VITALLY_API_KEY", "REPLACE")
    status, _ = asyncio.run(integration_health._check_vitally())
    assert status == "degraded"


def test_salesforce_create_record_requires_approval():
    from shared.mcp import salesforce_mcp
    from shared.governance import ApprovalRequired
    try:
        salesforce_mcp.create_record("Account", {"Name": "X"}, agent_name="test", approval_gate_id=None)
    except ApprovalRequired:
        pass
    else:
        raise AssertionError("expected ApprovalRequired")


def test_salesforce_update_record_with_approved_gate():
    from shared.mcp import salesforce_mcp
    from shared import governance
    gid = governance.create_approval_gate(
        agent_name="test", action_type="single_record_update", payload={}, justification=None
    )
    governance.decide_approval_gate(gid, approved=True, approver="UT")
    with patch.object(salesforce_mcp, "_sf", return_value={"id": "001xx", "success": True}):
        r = salesforce_mcp.update_record("Account", "001xx", {"Name": "Y"}, agent_name="test", approval_gate_id=gid)
    assert r.get("success") is True
