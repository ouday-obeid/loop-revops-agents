"""Handler routing tests — ping, unknown command, help, each subcommand stub."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from sqlalchemy import text

from agents.sales_reps.handler import SalesRepsAgent
from agents.sales_reps.main import handle, register_with_dispatcher
from shared.db.connection import get_engine
from shared.slack_dispatcher import _registry, dispatch


# ---------- direct handler ----------

def test_ping_returns_pong():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "ping"}))
    assert "pong" in out["text"].lower()
    assert "sales_reps" in out["text"]


def test_empty_text_is_ping():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": ""}))
    assert "pong" in out["text"].lower()


def test_unknown_command_returns_help():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "reticulate splines"}))
    assert "unknown command" in out["text"].lower()
    assert "ping" in out["text"]  # help listing
    assert "grade" in out["text"]


def test_help_command():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "help"}))
    assert "scorecard" in out["text"]
    assert "leaderboard" in out["text"]


# ---------- subcommand routing into stubs ----------

def test_grade_requires_meeting_id():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "grade"}))
    assert "usage" in out["text"].lower()


def test_grade_routes_to_grader():
    fake_result = {"text": "graded", "meeting_id": "MEETING_123", "graded": True}
    with patch("agents.sales_reps.call_grader.grader.grade_one",
               new=AsyncMock(return_value=fake_result)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "grade MEETING_123"}))
    m.assert_awaited_once_with("MEETING_123")
    assert out["meeting_id"] == "MEETING_123"


def test_batch_grade_requires_two_dates():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "batch-grade 2026-04-01"}))
    assert "usage" in out["text"].lower()


def test_batch_grade_routes_to_batch():
    fake_result = {"text": "batch graded", "from": "2026-04-01", "to": "2026-04-13", "graded": []}
    with patch("agents.sales_reps.call_grader.batch.grade_range",
               new=AsyncMock(return_value=fake_result)) as m:
        out = asyncio.run(SalesRepsAgent().run(
            "test", {"text": "batch-grade 2026-04-01 2026-04-13"}
        ))
    m.assert_awaited_once_with("2026-04-01", "2026-04-13")
    assert out["from"] == "2026-04-01"
    assert out["to"] == "2026-04-13"


def test_brief_requires_target():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "brief"}))
    assert "usage" in out["text"].lower()


def test_brief_routes():
    fake = {"text": "brief text", "target": "0061U00000ABC", "opportunity_id": "006X"}
    with patch("agents.sales_reps.pre_demo.brief_generator.generate",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "brief 0061U00000ABC"}))
    m.assert_awaited_once_with("0061U00000ABC")
    assert out["target"] == "0061U00000ABC"


def test_hygiene_no_filter():
    fake = {"text": "no issues", "ae_filter": None, "total_findings": 0,
            "totals_by_issue": {}, "findings_by_ae": {}}
    with patch("agents.sales_reps.pipeline_hygiene.run",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "hygiene"}))
    m.assert_awaited_once_with(ae_filter=None)
    assert out["ae_filter"] is None


def test_hygiene_with_ae_filter():
    fake = {"text": "no issues", "ae_filter": "ae@tryloop.ai", "total_findings": 0,
            "totals_by_issue": {}, "findings_by_ae": {}}
    with patch("agents.sales_reps.pipeline_hygiene.run",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run(
            "test", {"text": "hygiene ae@tryloop.ai"}
        ))
    m.assert_awaited_once_with(ae_filter="ae@tryloop.ai")
    assert out["ae_filter"] == "ae@tryloop.ai"


def test_leaderboard_default_ae():
    fake = {"text": "ae board", "kind": "ae", "week": "2026-W15", "rows": []}
    with patch("agents.sales_reps.leaderboards.snapshot",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "leaderboard"}))
    m.assert_awaited_once_with(kind="ae", week=None)
    assert out["kind"] == "ae"


def test_leaderboard_sdr():
    fake = {"text": "sdr board", "kind": "sdr", "week": "2026-W15", "rows": []}
    with patch("agents.sales_reps.leaderboards.snapshot",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "leaderboard sdr"}))
    m.assert_awaited_once_with(kind="sdr", week=None)
    assert out["kind"] == "sdr"


def test_leaderboard_invalid_kind():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "leaderboard nonsense"}))
    assert "usage" in out["text"].lower()


def test_scorecard_requires_email():
    out = asyncio.run(SalesRepsAgent().run("test", {"text": "scorecard"}))
    assert "usage" in out["text"].lower()


def test_scorecard_routes():
    fake = {"text": "scorecard body", "rep_email": "rep@tryloop.ai",
            "calls_graded": 0, "week_avg_pct": None, "trend_avg_pct": None,
            "best": None, "worst": None, "coaching_themes": [], "critical_miss_calls": 0}
    with patch("agents.sales_reps.scorecards.for_rep",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "scorecard rep@tryloop.ai"}))
    m.assert_awaited_once_with("rep@tryloop.ai")
    assert out["rep_email"] == "rep@tryloop.ai"


def test_sync_check_routes():
    fake = {"text": "sync OK", "calls_checked": 0, "breaks": [], "alert_suppressed": False}
    with patch("agents.sales_reps.momentum_sync_monitor.run_once",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "sync-check"}))
    m.assert_awaited_once_with()
    assert out["calls_checked"] == 0


def test_risk_sweep_routes():
    fake = {"text": "no signals", "total_signals": 0, "errors": [], "signals": []}
    with patch("agents.sales_reps.deal_risk.run_sweep",
               new=AsyncMock(return_value=fake)) as m:
        out = asyncio.run(SalesRepsAgent().run("test", {"text": "risk-sweep"}))
    m.assert_awaited_once_with()
    assert out["total_signals"] == 0


# ---------- dispatcher registration ----------

def test_register_with_dispatcher_adds_both_aliases():
    register_with_dispatcher()
    assert "sales_reps" in _registry
    assert "sales-reps" in _registry


def test_dispatch_via_hyphen_form():
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> sales-reps ping", {"user": "U123", "channel": "Cxx"}))
    assert "pong" in out["text"].lower()


def test_dispatch_via_underscore_form():
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> sales_reps ping", {"user": "U123", "channel": "Cxx"}))
    assert "pong" in out["text"].lower()


# ---------- run() lifecycle writes to agent_runs ----------

def test_agent_run_persists_to_db():
    asyncio.run(handle({"text": "ping"}))
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT status, agent_name FROM agent_runs "
                "WHERE agent_name='sales_reps' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "success"
    assert row[1] == "sales_reps"
