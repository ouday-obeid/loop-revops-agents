"""Scheduler ticks — dispatch table, CLI, and per-tick wiring."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from agents.sales_reps.call_grader import storage as grade_storage
from agents.sales_reps.scheduler import jobs
from shared.db.connection import get_engine


def _reset_grades() -> None:
    grade_storage.ensure_schema()
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM sales_reps_call_grades"))


@pytest.fixture(autouse=True)
def _clean_grades():
    _reset_grades()
    yield
    _reset_grades()


# --------------------------------------------------------------- dispatch table

def test_all_ticks_registered():
    assert set(jobs._TICKS) == {
        "grader_poll", "brief_scan", "hygiene_daily", "sync_check",
        "risk_sweep", "leaderboard_weekly", "scorecards_weekly",
    }


def test_run_unknown_tick_raises():
    with pytest.raises(SystemExit):
        jobs.run("nope")


def test_main_dispatches_tick_and_returns_zero(capsys):
    with patch.dict(jobs._TICKS, {"grader_poll": AsyncMock(return_value={"graded": []})}):
        rc = jobs.main(["grader_poll", "--json"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert json.loads(captured) == {"graded": []}


# --------------------------------------------------------------- grader_poll

def test_grader_poll_calls_grade_range_with_window():
    fake = {"graded": [{"meeting_id": "M1"}], "errors": []}
    with patch.object(jobs.grader_batch, "grade_range",
                      new=AsyncMock(return_value=fake)) as m:
        out = jobs.run("grader_poll")
    m.assert_awaited_once()
    args, kwargs = m.await_args
    # Two positional date strings.
    assert len(args) == 2
    assert kwargs.get("limit") == 50
    assert out["graded"][0]["meeting_id"] == "M1"


# --------------------------------------------------------------- brief_scan

def test_brief_scan_generates_brief_per_opp():
    candidates = [
        {"event": {"title": "Acme Demo"},
         "opportunity": {"Id": "006ABC", "Name": "Acme"}},
        {"event": {"title": "Unmatched"},
         "opportunity": None},  # no opp — skipped
    ]
    with patch.object(jobs.demo_trigger, "scan_upcoming", return_value=candidates), \
         patch.object(jobs.brief_generator, "generate",
                      new=AsyncMock(return_value={"text": "brief", "opportunity_id": "006ABC"})) as m:
        out = jobs.run("brief_scan")
    m.assert_awaited_once_with("006ABC", include_blocks=True)
    assert out["candidates_scanned"] == 2
    assert len(out["briefs"]) == 1


def test_brief_scan_isolates_per_candidate_errors():
    candidates = [
        {"event": {"title": "Good"}, "opportunity": {"Id": "006OK"}},
        {"event": {"title": "Bad"}, "opportunity": {"Id": "006FAIL"}},
    ]

    async def flaky(opp_id: str, *, include_blocks: bool = False):
        if opp_id == "006FAIL":
            raise RuntimeError("boom")
        return {"text": "ok", "opportunity_id": opp_id}

    with patch.object(jobs.demo_trigger, "scan_upcoming", return_value=candidates), \
         patch.object(jobs.brief_generator, "generate", side_effect=flaky):
        out = jobs.run("brief_scan")
    assert len(out["briefs"]) == 1
    assert len(out["errors"]) == 1
    assert out["errors"][0]["opportunity_id"] == "006FAIL"


# --------------------------------------------------------------- simple passthroughs

def test_hygiene_daily_calls_pipeline_hygiene_run():
    with patch.object(jobs.pipeline_hygiene, "run",
                      new=AsyncMock(return_value={"text": "ok"})) as m:
        out = jobs.run("hygiene_daily")
    m.assert_awaited_once_with(ae_filter=None)
    assert out["text"] == "ok"


def test_sync_check_calls_run_once():
    with patch.object(jobs.momentum_sync_monitor, "run_once",
                      new=AsyncMock(return_value={"text": "sync ok"})) as m:
        out = jobs.run("sync_check")
    m.assert_awaited_once_with()
    assert out["text"] == "sync ok"


def test_risk_sweep_calls_run_sweep():
    with patch.object(jobs.deal_risk, "run_sweep",
                      new=AsyncMock(return_value={"text": "no signals"})) as m:
        out = jobs.run("risk_sweep")
    m.assert_awaited_once_with()
    assert out["text"] == "no signals"


# --------------------------------------------------------------- weekly

def test_leaderboard_weekly_returns_both_kinds():
    async def fake_snapshot(kind: str = "ae", week: str | None = None):
        return {"kind": kind, "week": "2026-W15", "rows": []}
    with patch.object(jobs.leaderboards, "snapshot", side_effect=fake_snapshot):
        out = jobs.run("leaderboard_weekly")
    assert out["ae"]["kind"] == "ae"
    assert out["sdr"]["kind"] == "sdr"
    assert out["week"] == "2026-W15"


def test_scorecards_weekly_processes_distinct_reps():
    grade_storage.ensure_schema()
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        for i, email in enumerate(("a@x.com", "a@x.com", "b@x.com")):
            conn.execute(
                text(
                    """INSERT INTO sales_reps_call_grades
                       (meeting_id, rep_email, call_type, scorecard_type,
                        section_scores, percentage, graded_at)
                       VALUES (:m, :e, 'first_call', 'first_call', '{}', 80, :g)"""
                ),
                {"m": f"M-{i}-{email}", "e": email, "g": now},
            )

    async def fake_for_rep(email: str):
        return {"rep_email": email, "calls_graded": 1}

    with patch.object(jobs.scorecards, "for_rep", side_effect=fake_for_rep):
        out = jobs.run("scorecards_weekly")
    assert out["reps_processed"] == 2
    emails = {sc["rep_email"] for sc in out["scorecards"]}
    assert emails == {"a@x.com", "b@x.com"}


def test_scorecards_weekly_isolates_per_rep_error():
    grade_storage.ensure_schema()
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO sales_reps_call_grades
                   (meeting_id, rep_email, call_type, scorecard_type,
                    section_scores, percentage, graded_at)
                   VALUES ('M_FAIL', 'fail@x.com', 'first_call', 'first_call', '{}', 70, :g)"""
            ),
            {"g": now},
        )

    async def boom(email: str):
        raise RuntimeError("db hiccup")

    with patch.object(jobs.scorecards, "for_rep", side_effect=boom):
        out = jobs.run("scorecards_weekly")
    assert out["reps_processed"] == 1
    assert out["scorecards"][0]["error"].startswith("RuntimeError")
