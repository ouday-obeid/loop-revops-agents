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


# ----------------------------- Tier 12 FIX A: grader_poll guards FIREFLIES_API_KEY

def test_grader_poll_skips_when_no_fireflies_key(monkeypatch):
    """Missing FIREFLIES_API_KEY must not crash the tick — return a degraded
    skip status so launchd doesn't go into back-off."""
    monkeypatch.delenv("FIREFLIES_API_KEY", raising=False)
    import asyncio
    out = asyncio.run(jobs.grader_poll())
    assert out.get("skipped") == "no_fireflies_api_key"
    assert out.get("graded") == []
    assert out.get("errors") == []


def test_grader_poll_skips_when_fireflies_key_is_replace(monkeypatch):
    monkeypatch.setenv("FIREFLIES_API_KEY", "REPLACE")
    import asyncio
    out = asyncio.run(jobs.grader_poll())
    assert out.get("skipped") == "no_fireflies_api_key"


def test_grader_poll_proceeds_when_key_present(monkeypatch):
    """With a key set, grader_poll calls grade_range — verify it dispatches
    rather than skipping."""
    monkeypatch.setenv("FIREFLIES_API_KEY", "ff_real_key")
    import asyncio
    from agents.sales_reps.call_grader import batch as grader_batch

    async def _stub(start, end, limit):
        return {"graded": [{"meeting_id": "M1"}], "errors": []}

    with patch.object(grader_batch, "grade_range", side_effect=_stub):
        out = asyncio.run(jobs.grader_poll())
    assert out.get("skipped") is None
    assert len(out.get("graded", [])) == 1


# ----------------------------- Tier 12 FIX D: brief_scan idempotency by opp_id

def test_brief_scan_skips_already_briefed_opp(monkeypatch):
    """When audit_log shows a sales_reps_pre_demo_brief row for this opp_id
    in the last 6h, brief_scan must skip generation (idempotency keyed on
    opp_id, NOT event_id — a rescheduled demo doesn't regenerate)."""
    import asyncio
    from agents.sales_reps.scheduler import jobs as jobs_mod
    from agents.sales_reps.pre_demo import brief_generator
    from shared import governance

    # Pretend a brief was generated for opp 006XYZ in the recent past.
    governance.write_audit(
        agent_name="sales_reps",
        action="sales_reps_pre_demo_brief",
        target="sf:Opportunity:006XYZ",
        after={"people": 3},
    )

    # gcal trigger returns one candidate pointing at the same opp.
    candidates = [{
        "event": {"id": "evt-1", "title": "Demo with ACME"},
        "opportunity": {"Id": "006XYZ"},
    }]
    monkeypatch.setattr(jobs_mod.demo_trigger, "scan_upcoming", lambda: candidates)

    # brief_generator.generate must NOT be called.
    called = {"n": 0}

    async def _generate(opp_id, include_blocks=True):
        called["n"] += 1
        return {"text": "should not happen"}

    monkeypatch.setattr(brief_generator, "generate", _generate)

    out = asyncio.run(jobs_mod.brief_scan())
    assert called["n"] == 0
    assert len(out.get("skipped_already_briefed", [])) == 1
    assert out["skipped_already_briefed"][0]["opportunity_id"] == "006XYZ"


def test_brief_scan_proceeds_when_no_recent_brief(monkeypatch):
    """No recent audit row for this opp → brief_scan generates."""
    import asyncio
    from agents.sales_reps.scheduler import jobs as jobs_mod
    from agents.sales_reps.pre_demo import brief_generator

    candidates = [{
        "event": {"id": "evt-2", "title": "Demo with FRESH"},
        "opportunity": {"Id": "006FRESH"},
    }]
    monkeypatch.setattr(jobs_mod.demo_trigger, "scan_upcoming", lambda: candidates)

    async def _generate(opp_id, include_blocks=True):
        return {"text": "brief body", "opp_id": opp_id}

    monkeypatch.setattr(brief_generator, "generate", _generate)

    out = asyncio.run(jobs_mod.brief_scan())
    assert len(out.get("briefs", [])) == 1
    assert out["briefs"][0]["opportunity_id"] == "006FRESH"
    assert out.get("skipped_already_briefed") == []


# ----------------------------- Tier 12 FIX B: portable PK (no AUTOINCREMENT)

def test_storage_schema_omits_autoincrement():
    """AUTOINCREMENT is SQLite-only — Postgres rejects it. Schema must use
    plain INTEGER PRIMARY KEY so the same DDL works in both dialects."""
    from agents.sales_reps.call_grader import storage
    assert "AUTOINCREMENT" not in storage._SCHEMA_SQL
    assert "INTEGER PRIMARY KEY" in storage._SCHEMA_SQL


def test_storage_still_auto_increments_on_sqlite():
    """Sanity: SQLite gives implicit auto-increment for INTEGER PRIMARY KEY
    via rowid aliasing. Insert two rows, expect monotonically increasing ids."""
    from agents.sales_reps.call_grader import storage
    g1 = storage.upsert_grade({
        "meeting_id": "M_AI_1", "call_type": "demo", "scorecard_type": "demo",
        "section_scores": {"intro": 10}, "weighted_total": 80, "max_weighted": 100,
        "percentage": 80.0, "pass_fail": "pass",
    })
    g2 = storage.upsert_grade({
        "meeting_id": "M_AI_2", "call_type": "demo", "scorecard_type": "demo",
        "section_scores": {"intro": 8}, "weighted_total": 70, "max_weighted": 100,
        "percentage": 70.0, "pass_fail": "pass",
    })
    assert isinstance(g1, int) and isinstance(g2, int)
    assert g2 > g1


# ----------------------------- Tier 12 FIX C: pyproject testpaths (already done)

def test_pyproject_testpaths_includes_all_phase1_agents():
    """Phase 1 agent test dirs (top_of_funnel, sales_reps, onboarding, cs,
    revops_support, slt_metrics) must all be in pyproject.toml testpaths
    so `pytest` from repo root collects them. FIX C from Tier 12."""
    from pathlib import Path
    pyproject = (Path(__file__).resolve().parents[3] / "pyproject.toml").read_text()
    for agent in ("top_of_funnel", "sales_reps", "onboarding", "cs", "revops_support", "slt_metrics"):
        assert f'"agents/{agent}/tests"' in pyproject, f"agents/{agent}/tests not in testpaths"


def test_main_dispatches_tick_and_returns_zero(capsys):
    with patch.dict(jobs._TICKS, {"grader_poll": AsyncMock(return_value={"graded": []})}):
        rc = jobs.main(["grader_poll", "--json"])
    assert rc == 0
    captured = capsys.readouterr().out
    assert json.loads(captured) == {"graded": []}


# --------------------------------------------------------------- grader_poll

def test_grader_poll_calls_grade_range_with_window(monkeypatch):
    # FIX A in Tier 12 made grader_poll guard on missing FIREFLIES_API_KEY,
    # so the test env must set one for this dispatch path to fire.
    monkeypatch.setenv("FIREFLIES_API_KEY", "ff_test_key")
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
    # Tier 12 FIX D added opp-keyed idempotency: clear any prior audit row
    # for 006ABC (test_brief_generator may have written one earlier in the
    # same session) so brief_scan actually generates here.
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM audit_log WHERE target = :t"),
            {"t": "sf:Opportunity:006ABC"},
        )
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
