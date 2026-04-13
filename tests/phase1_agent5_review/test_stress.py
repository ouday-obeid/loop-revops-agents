"""Phase 1 Agent 5 post-phase review — adversarial stress tests.

All tests mock `salesforce_mcp._sf` and network paths. No real SF writes.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import text


@pytest.fixture(scope="module", autouse=True)
def _isolate_db():
    tmp = tempfile.mkdtemp(prefix="phase1_a5_review_")
    db_file = Path(tmp) / "test.db"
    os.environ["REVOPS_REPO_ROOT"] = tmp
    os.environ["REVOPS_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["REVOPS_KNOWLEDGE_BACKEND"] = "chromadb_local"
    os.environ["REVOPS_SECRETS_BACKEND"] = "dotenv"
    os.environ["SLACK_DEV_GUARD"] = "1"
    os.environ["SLACK_TEST_CHANNEL"] = "D_TEST_DM"
    os.environ.setdefault("SF_ORG_ALIAS", "salesops")
    from shared.db import connection as c
    c.reset_cache()
    c.init_schema()
    yield


# ------------- 1. Governance: bulk_update 500 blocks before _sf -------------

def test_bulk_update_500_raises_before_any_sf_call():
    from shared.mcp import salesforce_mcp
    from shared.governance import ApprovalRequired
    updates = [{"Id": f"001x{i:04d}", "Foo__c": "x"} for i in range(500)]
    with patch.object(salesforce_mcp, "_sf") as mock_sf:
        with pytest.raises(ApprovalRequired):
            salesforce_mcp.bulk_update(
                "Account", updates, agent_name="revops_support", approval_gate_id=None,  # type: ignore[arg-type]
            )
        mock_sf.assert_not_called()


# ------------- 2. Schema-delete cooldown sets approved_primary -------------

def test_schema_delete_approval_enters_cooldown():
    from shared.governance import (
        create_approval_gate, decide_approval_gate, get_approval_gate,
    )
    gate_id = create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete",
        payload={"field": "Opportunity.Legacy__c"},
        justification="unused since 2024-06",
    )
    decide_approval_gate(gate_id, approved=True, approver="U07P4GX9YLQ")
    gate = get_approval_gate(gate_id)
    assert gate is not None
    assert gate["status"] == "approved_primary", f"expected approved_primary, got {gate['status']}"
    assert gate["cooldown_until"] is not None, "cooldown_until must be set"


# ------------- 3. Rate limits (hard): sf_bulk_update_hourly blocks ----------

def test_hard_rate_limit_blocks_after_cap():
    from shared.governance import check_rate_limit, RateLimitExceeded, RATE_LIMITS
    limit = RATE_LIMITS["sf_bulk_update_hourly"]
    # fire limit-many calls should be fine
    for _ in range(limit):
        check_rate_limit("sf_bulk_update_hourly", window_seconds=3600)
    # the one after should raise
    with pytest.raises(RateLimitExceeded):
        check_rate_limit("sf_bulk_update_hourly", window_seconds=3600)


# ------------- 4. Rate limits (soft): schema_changes_weekly warns ----------

def test_soft_rate_limit_does_not_raise(caplog):
    import logging
    from shared.governance import check_rate_limit, RATE_LIMITS
    bucket = "revops_schema_changes_weekly"
    limit = RATE_LIMITS[bucket]
    for _ in range(limit):
        check_rate_limit(bucket, window_seconds=604800)
    caplog.set_level(logging.WARNING)
    # One past cap — soft should log WARN and return count, not raise
    count = check_rate_limit(bucket, window_seconds=604800)
    assert count == limit + 1
    assert any("SOFT breach" in rec.message for rec in caplog.records), (
        "expected SOFT breach warning in logs"
    )


# ------------- 5. Slack DEV_GUARD refuses non-test channel -----------------

def test_dev_guard_blocks_non_test_channel():
    from shared.slack_dispatcher import SlackSender
    sender = SlackSender(client=object())  # guard should short-circuit before client use
    result = sender.send("#client-lula-general", "hi")
    assert result.get("blocked") is True
    assert result.get("ok") is False


# ------------- 6. SOQL engine rejects DML + clamps missing LIMIT -----------

def test_soql_engine_rejects_dml_and_adds_limit():
    from agents.revops_support.query import soql_engine
    with pytest.raises(soql_engine.SOQLError):
        soql_engine.run("UPDATE Account SET Name = 'x'")
    with pytest.raises(soql_engine.SOQLError):
        soql_engine.run("DELETE FROM Contact")
    # LIMIT auto-added
    with patch(
        "agents.revops_support.query.soql_engine.salesforce_mcp.soql_query",
        return_value={"records": []},
    ) as mock_q:
        soql_engine.run("SELECT Id FROM Account", default_limit=25)
        called_q = mock_q.call_args[0][0]
        assert "LIMIT 25" in called_q.upper()


# ------------- 7. Describe cache: TTL refresh, bust, vacuum ----------------

def test_describe_cache_ttl_and_bust():
    from datetime import timedelta
    from shared.db.connection import get_engine
    from agents.revops_support.query import describe_cache
    engine = get_engine()
    # seed stale row (25h old)
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM describe_cache WHERE org_alias = :a AND sobject = :s"),
            {"a": "salesops", "s": "Account"},
        )
        conn.execute(
            text(
                "INSERT INTO describe_cache (org_alias, sobject, describe_json, fetched_at) "
                "VALUES (:a, :s, :j, :t)"
            ),
            {"a": "salesops", "s": "Account", "j": '{"old": true}', "t": stale},
        )
    with patch(
        "agents.revops_support.query.describe_cache.salesforce_mcp.describe_sobject",
        return_value={"fresh": True},
    ):
        fresh = describe_cache.get("Account", intent="read")
        assert fresh.get("fresh") is True, "stale row should have been refreshed"
    # bust path
    removed = describe_cache.bust(sobjects=["Account"], alias="salesops")
    assert removed >= 1
    # vacuum path — insert very old row, vacuum drops it
    very_old = datetime.now(timezone.utc) - timedelta(days=30)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO describe_cache (org_alias, sobject, describe_json, fetched_at) "
                "VALUES (:a, :s, :j, :t)"
            ),
            {"a": "salesops", "s": "Zombie__c", "j": "{}", "t": very_old},
        )
    dropped = describe_cache.vacuum_stale()
    assert dropped >= 1


# ------------- 8. Dispatcher: underscore + hyphen both route ---------------

def test_dispatcher_routing_variants():
    import asyncio
    from shared import slack_dispatcher
    from agents.revops_support import agent as revops_agent
    slack_dispatcher._registry.clear()
    slack_dispatcher.register("revops_support", revops_agent.handle)
    # hyphen — parsed against underscore registry key; parse_command lowercases
    # and splits. The OO layer is responsible for hyphen→underscore; verify
    # that underscore matches directly.
    got_agent, rest = slack_dispatcher.parse_command("<@U1> oo revops_support ping")
    assert got_agent == "revops_support"
    # Direct handler call
    result = asyncio.run(revops_agent.handle({"text": "ping"}))
    assert "pong" in result["text"].lower()


# ------------- 9. Canned queries: all 8 registered -------------------------

def test_canned_registry_has_eight():
    from agents.revops_support.query import canned
    assert len(canned.REGISTRY) == 8
    for name, fn in canned.REGISTRY.items():
        assert callable(fn), name


# ------------- 10. Slack handle_gate_decision bypasses cooldown (REGRESSION)

def test_slack_handle_gate_decision_bypasses_cooldown_BUG():
    """DEFECT CHECK: plan requires slack_dispatcher.handle_gate_decision to
    route through governance.decide_approval_gate so sf_schema_delete enters
    cooldown. Currently it uses raw SQL and writes status='approved' directly,
    skipping the 24h cooling period. This test documents the bug; if the bug
    is fixed, flip the assertion."""
    from shared.governance import create_approval_gate, get_approval_gate
    from shared import slack_dispatcher
    gate_id = create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete",
        payload={"field": "Opportunity.Legacy2__c"},
        justification="test",
    )
    slack_dispatcher.handle_gate_decision(gate_id, approved=True, approver="O")
    gate = get_approval_gate(gate_id)
    # Expected (plan-correct): "approved_primary" with cooldown_until
    # Actual (bug): "approved" with no cooldown.
    # Assert the BUG state so the test documents the regression. When fixed,
    # flip to: assert gate["status"] == "approved_primary".
    assert gate["status"] == "approved", (
        "If this test now fails with status=approved_primary, the bug is FIXED — "
        "flip the assertion and delete this comment."
    )
    assert gate["cooldown_until"] is None, "cooldown NOT set — governance bypass"
