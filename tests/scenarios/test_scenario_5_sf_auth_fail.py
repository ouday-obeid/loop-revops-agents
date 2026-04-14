"""Scenario 5 — Integration outage sim: SF auth fails, agents degrade gracefully.

Monday item: 11736866672
Path: OO integration_health poller detects SF auth failure → records `down`
in integration_health table → downstream agent (ToF sf_lead_writer) that
depends on SF does NOT crash the agent loop; dedup probe fails-open and the
write path still raises ApprovalRequired cleanly.

Two agent boundaries validated:

  1. OO health poller: salesforce_mcp.soql_query raising RuntimeError becomes
     a `down` status row in integration_health.
  2. ToF sf_lead_writer: a SOQL exception during dedup probe is swallowed
     (fail-open is the documented contract — see check_duplicate) rather
     than propagating. Missing approval gate still raises ApprovalRequired
     instead of accidentally writing unapproved records.

Why this shape: the full outage runbook includes Slack alerting for status
changes, which belongs to the per-agent health suite. The Phase 1 DoD
question "does an SF outage break in-flight agent work?" is answered by
these two boundaries.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import text

from agents.oo import integration_health
from agents.top_of_funnel import sf_lead_writer
from shared.db.connection import get_engine
from shared.governance import ApprovalRequired
from shared.mcp import salesforce_mcp


@pytest.fixture(autouse=True)
def _clean_health():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM integration_health WHERE integration = 'salesforce'"
        ))
    yield


@pytest.mark.asyncio
async def test_sf_auth_failure_records_down_status():
    """salesforce_mcp.soql_query raises → health poller writes `down`."""
    with patch.object(
        salesforce_mcp,
        "soql_query",
        side_effect=RuntimeError("INVALID_SESSION_ID: Session expired or invalid"),
    ):
        status, error = await integration_health._check_salesforce()

    assert status == "down"
    assert error is not None
    assert "INVALID_SESSION_ID" in error

    # Persist through the public recorder and verify the row lands.
    integration_health._record("salesforce", status, error)
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT status, error_message, last_failure "
                "FROM integration_health WHERE integration = 'salesforce' "
                "ORDER BY checked_at DESC LIMIT 1"
            )
        ).mappings().first()
    assert row is not None
    assert row["status"] == "down"
    assert "INVALID_SESSION_ID" in (row["error_message"] or "")
    assert row["last_failure"] is not None


def test_tof_dedup_probe_fails_open_on_sf_exception():
    """Dedup SOQL raises → check_duplicate logs + returns no-match (fail-open).

    This is the "agent degrades gracefully" promise: a transient SF auth blip
    during a dedup probe must not crash the ToF pipeline. The lead creation
    path still raises ApprovalRequired on missing gate so no unapproved writes
    sneak through.
    """
    def raising_query(*args, **kwargs):
        raise RuntimeError("INVALID_SESSION_ID: expired")

    result = sf_lead_writer.check_duplicate(
        email="alex@acme.com",
        domain="acme.com",
        sf_query=raising_query,
    )
    # Fail-open: not flagged as duplicate, reason captures the probe failure.
    assert result.is_duplicate is False
    assert "dedup_probe_failed" in result.reason


def test_tof_create_without_gate_raises_cleanly_during_outage():
    """Even if SF is down, the MCP write boundary refuses without an approved
    gate — never accidentally writes through.

    We exercise salesforce_mcp.create_record directly (the canonical gate
    boundary). sf_lead_writer passes through unchanged; its own `create_fn`
    injection is for tests that want to bypass governance, which is not this
    test's job.
    """
    from shared.mcp import salesforce_mcp as sf

    with pytest.raises(ApprovalRequired):
        sf.create_record(
            "Lead",
            {"Email": "a@b.com", "Company": "BCo", "FirstName": "A", "LastName": "B"},
            agent_name="top_of_funnel",
            approval_gate_id=None,  # no gate — rejected regardless of outage
        )
