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


# ----------------------------- Vitally freshness path

class _FakeResp:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body


class _FakeHttpxClient:
    """Mimics httpx.Client context manager + .get returning _FakeResp."""

    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, *args, **kwargs):
        return self._resp


def _patch_httpx(monkeypatch, integration_health, resp):
    monkeypatch.setattr(
        integration_health, "httpx",
        type("X", (), {"Client": lambda *a, **kw: _FakeHttpxClient(resp), "Response": _FakeResp}),
    )


def test_check_vitally_healthy_when_recent(monkeypatch):
    from agents.oo import integration_health
    from datetime import datetime, timezone
    monkeypatch.setenv("VITALLY_API_KEY", "vk_fake")
    fresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    body = {"results": [{"lastInboundConnectionAt": fresh}]}
    _patch_httpx(monkeypatch, integration_health, _FakeResp(200, body))
    status, err = asyncio.run(integration_health._check_vitally())
    assert status == "healthy"
    assert err is None


def test_check_vitally_degraded_when_stale(monkeypatch):
    from agents.oo import integration_health
    from datetime import datetime, timedelta, timezone
    monkeypatch.setenv("VITALLY_API_KEY", "vk_fake")
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    body = {"results": [{"lastInboundConnectionAt": stale}]}
    _patch_httpx(monkeypatch, integration_health, _FakeResp(200, body))
    status, err = asyncio.run(integration_health._check_vitally())
    assert status == "degraded"
    assert "stale" in (err or "")


def test_check_vitally_down_on_auth_fail(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("VITALLY_API_KEY", "bad")
    _patch_httpx(monkeypatch, integration_health, _FakeResp(401))
    status, err = asyncio.run(integration_health._check_vitally())
    assert status == "down"
    assert "auth" in (err or "").lower()


# ----------------------------- Clay / Apollo / Nooks (auth check pattern)

def test_check_clay_no_key(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.delenv("CLAY_API_KEY", raising=False)
    status, _ = asyncio.run(integration_health._check_clay())
    assert status == "degraded"


def test_check_clay_healthy(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("CLAY_API_KEY", "ck_fake")
    _patch_httpx(monkeypatch, integration_health, _FakeResp(200, {"workflows": []}))
    status, _ = asyncio.run(integration_health._check_clay())
    assert status == "healthy"


def test_check_clay_down_on_403(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("CLAY_API_KEY", "expired")
    _patch_httpx(monkeypatch, integration_health, _FakeResp(403))
    status, err = asyncio.run(integration_health._check_clay())
    assert status == "down"
    assert "auth" in (err or "").lower()


def test_check_apollo_no_key(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    status, _ = asyncio.run(integration_health._check_apollo())
    assert status == "degraded"


def test_check_apollo_healthy(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("APOLLO_API_KEY", "ap_fake")
    _patch_httpx(monkeypatch, integration_health, _FakeResp(200, {"status": "ok"}))
    status, _ = asyncio.run(integration_health._check_apollo())
    assert status == "healthy"


def test_check_nooks_no_key(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.delenv("NOOKS_API_KEY", raising=False)
    status, _ = asyncio.run(integration_health._check_nooks())
    assert status == "degraded"


def test_check_nooks_healthy(monkeypatch):
    from agents.oo import integration_health
    monkeypatch.setenv("NOOKS_API_KEY", "nk_fake")
    _patch_httpx(monkeypatch, integration_health, _FakeResp(200, {"id": "u1"}))
    status, _ = asyncio.run(integration_health._check_nooks())
    assert status == "healthy"


# ----------------------------- _record INSERT-on-change guard

def test_record_inserts_first_time_then_skips_unchanged_then_inserts_on_change():
    from agents.oo import integration_health
    from sqlalchemy import text as sql_text
    from shared.db.connection import get_engine

    integ = f"_test_unchanged_{id(test_record_inserts_first_time_then_skips_unchanged_then_inserts_on_change)}"

    def _count():
        with get_engine().begin() as conn:
            return conn.execute(
                sql_text("SELECT COUNT(*) FROM integration_health WHERE integration = :i"),
                {"i": integ},
            ).scalar()

    assert _count() == 0
    # First-ever record: insert, return None (no prior status).
    assert integration_health._record(integ, "healthy") is None
    assert _count() == 1
    # Same status repeated: no insert, return None.
    assert integration_health._record(integ, "healthy") is None
    assert _count() == 1
    # Status change: insert, return prior status.
    assert integration_health._record(integ, "degraded", "test fail") == "healthy"
    assert _count() == 2
    # Same again: no insert.
    assert integration_health._record(integ, "degraded") is None
    assert _count() == 2


# ----------------------------- DM alert on transition-to-unhealthy

def test_poll_alerts_o_dm_on_transition_to_unhealthy(monkeypatch):
    from agents.oo import integration_health

    sent: list[dict] = []

    class _Capture:
        def __init__(self, client=None): pass

        def send(self, channel, text_, blocks=None):
            sent.append({"channel": channel, "text": text_})
            return {"ok": True, "ts": "1.0", "channel": channel}

    monkeypatch.setattr("shared.slack_dispatcher.SlackSender", _Capture)
    monkeypatch.setenv("SLACK_TEST_CHANNEL", "U_O_TEST")

    integration_health._alert_o_dm("vitally", "healthy", "down", "auth failed (HTTP 401)")
    assert len(sent) == 1
    assert sent[0]["channel"] == "U_O_TEST"
    assert "vitally" in sent[0]["text"]
    assert "healthy" in sent[0]["text"] and "down" in sent[0]["text"]


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
