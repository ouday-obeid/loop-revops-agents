"""Per-rep scorecard — helpers, renderer, and public API."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from agents.sales_reps import scorecards as sc
from agents.sales_reps.call_grader import storage as grade_storage
from shared.db.connection import get_engine


# --------------------------------------------------------------- fixture helpers

def _reset_grades() -> None:
    grade_storage.ensure_schema()
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM sales_reps_call_grades"))


def _insert_grade(
    *,
    meeting_id: str,
    rep_email: str = "rep@tryloop.ai",
    call_type: str = "first_call",
    percentage: float | None = 80.0,
    pass_fail: str | None = "pass",
    coaching_summary: str | None = None,
    critical_misses: list[str] | None = None,
    graded_at: datetime | None = None,
) -> None:
    graded_at = graded_at or datetime.now(timezone.utc)
    grade_storage.ensure_schema()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO sales_reps_call_grades
                   (meeting_id, rep_email, call_type, scorecard_type, section_scores,
                    percentage, pass_fail, coaching_summary, critical_misses, graded_at)
                   VALUES (:m, :e, :ct, :sc, :ss, :p, :pf, :cs, :cm, :g)"""
            ),
            {
                "m": meeting_id, "e": rep_email,
                "ct": call_type, "sc": call_type, "ss": "{}",
                "p": percentage, "pf": pass_fail,
                "cs": coaching_summary,
                "cm": json.dumps(critical_misses or []),
                "g": graded_at,
            },
        )


@pytest.fixture(autouse=True)
def _clean_grades():
    _reset_grades()
    yield
    _reset_grades()


# --------------------------------------------------------------- _parse_list_field

def test_parse_list_field_none_returns_empty():
    assert sc._parse_list_field(None) == []


def test_parse_list_field_list_stringifies():
    assert sc._parse_list_field(["a", 1]) == ["a", "1"]


def test_parse_list_field_json_string():
    assert sc._parse_list_field('["x", "y"]') == ["x", "y"]


def test_parse_list_field_bad_json_returns_empty():
    assert sc._parse_list_field("not json") == []


def test_parse_list_field_non_list_json_returns_empty():
    assert sc._parse_list_field('{"k":"v"}') == []


# --------------------------------------------------------------- _avg / _best_worst

def _summary(**kwargs) -> sc.CallSummary:
    base = dict(
        meeting_id="M1", call_type="first_call", percentage=80.0,
        pass_fail="pass", call_date=None, coaching_summary=None,
        critical_misses=[],
    )
    base.update(kwargs)
    return sc.CallSummary(**base)


def test_avg_rounds_to_one_decimal():
    rows = [_summary(percentage=80.0), _summary(percentage=75.0)]
    assert sc._avg(rows) == 77.5


def test_avg_none_when_empty():
    assert sc._avg([]) is None


def test_avg_skips_none_percentages():
    rows = [_summary(percentage=None), _summary(percentage=60.0)]
    assert sc._avg(rows) == 60.0


def test_best_worst_returns_top_and_bottom():
    rows = [
        _summary(meeting_id="LOW", percentage=40.0),
        _summary(meeting_id="HIGH", percentage=95.0),
        _summary(meeting_id="MID", percentage=70.0),
    ]
    best, worst = sc._best_worst(rows)
    assert best.meeting_id == "HIGH"
    assert worst.meeting_id == "LOW"


def test_best_worst_empty_when_no_grades():
    best, worst = sc._best_worst([_summary(percentage=None)])
    assert best is None and worst is None


# --------------------------------------------------------------- _coaching_themes

def test_coaching_themes_dedupes_and_truncates():
    rows = [
        _summary(meeting_id=f"M{i}", coaching_summary="Lead with discovery questions" * 20)
        for i in range(3)
    ]
    themes = sc._coaching_themes(rows, limit=5)
    # All three share the same first-80-char prefix → dedup to 1.
    assert len(themes) == 1
    assert len(themes[0]) <= 240


def test_coaching_themes_respects_limit():
    rows = [
        _summary(meeting_id="A", coaching_summary="Probe on timeline"),
        _summary(meeting_id="B", coaching_summary="Confirm economic buyer"),
        _summary(meeting_id="C", coaching_summary="Stop pitching before discovery"),
        _summary(meeting_id="D", coaching_summary="Ask for the meeting"),
    ]
    themes = sc._coaching_themes(rows, limit=2)
    assert len(themes) == 2


def test_coaching_themes_skips_blank():
    rows = [_summary(coaching_summary="   "), _summary(coaching_summary=None)]
    assert sc._coaching_themes(rows) == []


# --------------------------------------------------------------- _render

def test_render_empty_week_message():
    out = sc._render("rep@x.com", [], None, None, None, None, [])
    assert "No graded calls" in out
    assert "rep@x.com" in out


def test_render_shows_week_avg_and_trend_arrow_up():
    rows = [_summary(percentage=90.0)]
    out = sc._render("rep@x.com", rows, 90.0, 70.0, rows[0], rows[0], [])
    assert "avg 90%" in out
    assert "▲" in out


def test_render_trend_arrow_flat_when_within_threshold():
    rows = [_summary(percentage=80.0)]
    # delta = 80 - 79 = 1.0, within ±2 → flat.
    out = sc._render("rep@x.com", rows, 80.0, 79.0, rows[0], rows[0], [])
    assert "→" in out


def test_render_trend_arrow_down():
    rows = [_summary(percentage=60.0)]
    out = sc._render("rep@x.com", rows, 60.0, 80.0, rows[0], rows[0], [])
    assert "▼" in out


def test_render_highlights_best_and_worst_when_distinct():
    best = _summary(meeting_id="BEST", percentage=95.0)
    worst = _summary(meeting_id="WORST", percentage=40.0)
    out = sc._render("r@x.com", [best, worst], 67.5, None, best, worst, [])
    assert "Best" in out and "BEST" in out
    assert "Watch" in out and "WORST" in out


def test_render_flags_critical_misses():
    rows = [_summary(meeting_id="C1", critical_misses=["no discovery", "no next step"])]
    out = sc._render("r@x.com", rows, 60.0, None, rows[0], rows[0], [])
    assert "Critical misses" in out
    assert "C1" in out


def test_render_includes_coaching_themes():
    rows = [_summary(percentage=70.0)]
    themes = ["Ask deeper pain questions"]
    out = sc._render("r@x.com", rows, 70.0, None, rows[0], rows[0], themes)
    assert "Coaching themes" in out
    assert "Ask deeper pain questions" in out


def test_render_by_call_type_section():
    rows = [_summary(call_type="first_call"), _summary(meeting_id="M2", call_type="follow_up")]
    out = sc._render("r@x.com", rows, 80.0, None, rows[0], rows[0], [])
    assert "This week by call type" in out
    assert "first_call" in out
    assert "follow_up" in out


# --------------------------------------------------------------- for_rep

def test_for_rep_empty_email_returns_usage():
    out = asyncio.run(sc.for_rep(""))
    assert out["error"] == "empty_rep"
    assert "Usage" in out["text"]


def test_for_rep_no_grades_returns_zero_count():
    out = asyncio.run(sc.for_rep("nobody@tryloop.ai"))
    assert out["calls_graded"] == 0
    assert out["week_avg_pct"] is None
    assert "No graded calls" in out["text"]


def test_for_rep_lowercases_email():
    _insert_grade(meeting_id="M1", rep_email="rep@tryloop.ai", percentage=80.0)
    out = asyncio.run(sc.for_rep("REP@TryLoop.AI"))
    assert out["rep_email"] == "rep@tryloop.ai"
    assert out["calls_graded"] == 1


def test_for_rep_happy_path_aggregates():
    now = datetime.now(timezone.utc)
    _insert_grade(
        meeting_id="W1", rep_email="rep@tryloop.ai",
        percentage=85.0, graded_at=now, coaching_summary="Probe timeline",
    )
    _insert_grade(
        meeting_id="W2", rep_email="rep@tryloop.ai",
        percentage=75.0, graded_at=now, critical_misses=["skipped recap"],
    )
    out = asyncio.run(sc.for_rep("rep@tryloop.ai"))
    assert out["calls_graded"] == 2
    assert out["week_avg_pct"] == 80.0
    assert out["best"]["meeting_id"] == "W1"
    assert out["worst"]["meeting_id"] == "W2"
    assert out["critical_miss_calls"] == 1
    assert "Probe timeline" in out["coaching_themes"]


def test_for_rep_trend_window_larger_than_week():
    now = datetime.now(timezone.utc)
    # This week: 90%
    _insert_grade(
        meeting_id="THIS", rep_email="rep@tryloop.ai",
        percentage=90.0, graded_at=now,
    )
    # Older, within 28-day trend: 60%
    _insert_grade(
        meeting_id="OLD", rep_email="rep@tryloop.ai",
        percentage=60.0, graded_at=now - timedelta(days=20),
    )
    out = asyncio.run(sc.for_rep("rep@tryloop.ai"))
    assert out["week_avg_pct"] == 90.0
    # trend average spans both rows → 75.
    assert out["trend_avg_pct"] == 75.0


def test_for_rep_degrades_on_db_error():
    with patch.object(sc, "_query_grades", side_effect=RuntimeError("db down")):
        out = asyncio.run(sc.for_rep("rep@tryloop.ai"))
    assert "query failed" in out["text"].lower()
    assert out["rep_email"] == "rep@tryloop.ai"
    assert out["error"]
