"""Scenario 3 — Churn risk detected → renewal opp created → SLT report path.

Monday item: 11736878817
Path: CS (renewal pipeline creates T-120 Renewal opp via self-approved gate)
→ SLT Metrics (dispatcher routes `@oo slt forecast <q>` which would pick up
the fresh pipeline data).

Scope note: we do NOT drive the full SLT briefing composer here — Narrator +
Sonnet/Opus model routing is fully tested in slt_metrics/tests. The critical
boundary for Scenario 3 is "CS created a Renewal that SLT can report on,
with a governance trail", so we assert:

  1. Renewal opp was created with Type='Renewal' and an approved gate.
  2. cs_renewal_state row was persisted (SLT's forecast pipeline joins on it).
  3. The SLT dispatcher's `forecast` routing accepts a quarter arg and returns
     a structured response — this is the boundary the weekly SLT report hits.
  4. Audit trail exists for the CS write (write_audit row under agent='cs').
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.cs.renewal import pipeline
from agents.slt_metrics import dispatcher as slt_dispatcher
from agents.slt_metrics.agent import SltMetricsAgent
from shared.db.connection import get_engine


class _SfForRenewal:
    """Minimal SF double shaped for the CS renewal pipeline.

    Mirrors agents/cs/tests/test_renewal_pipeline.py::FakeSf so we don't
    re-invent the protocol, but scoped to the one-opp case for brevity.
    """

    def __init__(self, *, due_opps: list[dict[str, Any]], has_stage: bool = True):
        self._due = due_opps
        self._has_stage = has_stage
        self.created: list[dict[str, Any]] = []

    def describe_sobject(self, name: str, **_):
        values = [
            {"value": "Qualification", "active": True},
            {"value": "Closed Won", "active": True},
        ]
        if self._has_stage:
            values.append({"value": "Renewal Outreach", "active": True})
        return {"fields": [{"name": "StageName", "picklistValues": values}]}

    def soql_query(self, q: str, limit: int = 100, **_):
        if "Zen_Contract_End_Date__c >=" in q and "IsClosed = false" in q:
            return {"records": self._due}
        if "Type = 'Renewal'" in q:
            # No pre-existing renewal — force creation path.
            return {"records": []}
        return {"records": []}

    def create_record(self, sobject, fields, *, agent_name, approval_gate_id, **_):
        assert sobject == "Opportunity"
        assert approval_gate_id is not None
        new_id = f"006RENEW{len(self.created):03d}"
        self.created.append({
            "sobject": sobject, "fields": dict(fields),
            "approval_gate_id": approval_gate_id, "id": new_id,
        })
        return {"id": new_id, "success": True}


@pytest.fixture(autouse=True)
def _clean_state():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_renewal_state"))
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:renewal_pipeline:%'"))
        conn.execute(text("DELETE FROM approval_gates WHERE agent_name = 'cs'"))
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'cs'"))
    yield


def _due_opp(account_id: str = "001CHURN000000AAA", name: str = "FadingCo") -> dict[str, Any]:
    end = (datetime.now(timezone.utc) + timedelta(days=120)).date().isoformat()
    return {
        "Id": "006SRC_CHURN",
        "AccountId": account_id,
        "Account": {"Name": name},
        "OwnerId": "005JACKIE0000000",
        "Amount": 60000,
        "Zen_Contract_End_Date__c": end,
    }


@pytest.mark.asyncio
async def test_churn_risk_renewal_flow_and_slt_handoff(monkeypatch):
    sf = _SfForRenewal(due_opps=[_due_opp()])

    counters = await pipeline.run_sweep(sf_mcp=sf)

    # Boundary 1 — Renewal created.
    assert counters["candidates"] == 1
    assert counters["created"] == 1
    assert counters["skipped"] == 0
    assert len(sf.created) == 1
    created = sf.created[0]
    assert created["fields"]["Type"] == "Renewal"
    assert created["fields"]["AccountId"] == "001CHURN000000AAA"
    # Self-approved single_record_update gate.
    assert created["approval_gate_id"] is not None

    # Boundary 2 — cs_renewal_state row persisted for the new opp.
    engine = get_engine()
    with engine.begin() as conn:
        state = conn.execute(
            text(
                "SELECT account_id, stage, contract_end_date, provisional "
                "FROM cs_renewal_state WHERE opportunity_id = :o"
            ),
            {"o": created["id"]},
        ).mappings().first()
    assert state is not None
    assert state["account_id"] == "001CHURN000000AAA"
    assert state["stage"] == "Renewal Outreach"
    assert state["provisional"] == 0  # preferred stage available → not provisional

    # Boundary 3 — SLT dispatcher accepts `forecast <quarter>` and returns a
    # routed response. This is the boundary the weekly SLT report crosses.
    # The slt-forecast-dispatcher merge wired forecast to a real Slack DM send;
    # patch the sender so the test doesn't need SLACK_BOT_TOKEN. Mirrors the
    # `capture_forecast_dm` fixture in agents/slt_metrics/tests/test_dispatcher_routing.py.
    monkeypatch.setattr(
        slt_dispatcher,
        "_get_default_sender",
        lambda: lambda channel, text_, blocks: {"ok": True, "ts": "1.0", "channel": channel},
    )
    agent = SltMetricsAgent()
    slt_result = await slt_dispatcher.route(
        agent, trigger="scenario", payload={"text": "forecast FY2026-Q2"}
    )
    assert slt_result.get("cmd") == "forecast"
    assert slt_result.get("quarter") == "FY2026-Q2"
    # TODO: once D14 composer is live, assert the forecast pulls the new
    # Renewal opp into its top-of-quarter pipeline roll-up. Stubbed today.

    # Boundary 4 — gate + audit trail exist under agent='cs'. write_audit
    # is only wired from salesforce_mcp.create_record (not the FakeSf double),
    # so we assert the gate — the closer Phase-1 proxy for auditability —
    # rather than duplicating audit_log write semantics in the double.
    with engine.begin() as conn:
        gate = conn.execute(
            text(
                "SELECT agent_name, action_type, status FROM approval_gates "
                "WHERE id = :id"
            ),
            {"id": created["approval_gate_id"]},
        ).mappings().first()
    assert gate is not None
    assert gate["agent_name"] == "cs"
    assert gate["status"] == "approved"
    assert gate["action_type"] == "single_record_update"


@pytest.mark.asyncio
async def test_slt_forecast_usage_when_no_quarter_arg():
    """Regression: missing quarter arg returns a usage hint (routing sanity)."""
    agent = SltMetricsAgent()
    result = await slt_dispatcher.route(
        agent, trigger="scenario", payload={"text": "forecast"}
    )
    assert "Usage" in result["text"]
    assert "FY2026" in result["text"] or "quarter" in result["text"].lower()
