"""Weekly leaderboards — week math, grade aggregation, SF rollups, render, snapshot."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from agents.sales_reps import leaderboards as lb
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
    rep_email: str,
    rep_name: str = "Rep",
    call_type: str = "first_call",
    percentage: float = 80.0,
    graded_at: datetime | None = None,
    critical_misses: list[str] | None = None,
) -> None:
    graded_at = graded_at or datetime.now(timezone.utc)
    grade_storage.ensure_schema()
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO sales_reps_call_grades
                   (meeting_id, rep_email, rep_name, call_type, scorecard_type,
                    section_scores, percentage, critical_misses, graded_at)
                   VALUES (:m, :e, :n, :ct, :sc, :ss, :p, :cm, :g)"""
            ),
            {
                "m": meeting_id, "e": rep_email, "n": rep_name,
                "ct": call_type, "sc": call_type, "ss": "{}",
                "p": percentage,
                "cm": json.dumps(critical_misses or []),
                "g": graded_at,
            },
        )


@pytest.fixture(autouse=True)
def _clean_grades():
    _reset_grades()
    yield
    _reset_grades()


# --------------------------------------------------------------- iso_week_bounds

def test_iso_week_bounds_current_week_when_none():
    start, end, label = lb.iso_week_bounds(None)
    assert end - start == timedelta(days=7)
    # Start must be Monday.
    assert start.weekday() == 0
    assert label.startswith(str(start.year)) or "-W" in label


def test_iso_week_bounds_parses_explicit_week():
    start, end, label = lb.iso_week_bounds("2026-W15")
    assert label == "2026-W15"
    assert start.tzinfo is timezone.utc
    assert (end - start).days == 7
    assert start.weekday() == 0


def test_iso_week_bounds_bad_format_raises():
    with pytest.raises(ValueError):
        lb.iso_week_bounds("2026-15")  # no "W"


# --------------------------------------------------------------- _grade_stats_by_rep

def test_grade_stats_groups_and_averages():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=3)
    end = now + timedelta(days=1)
    _insert_grade(meeting_id="M1", rep_email="a@x.com", percentage=80.0, graded_at=now)
    _insert_grade(meeting_id="M2", rep_email="a@x.com", percentage=60.0, graded_at=now)
    _insert_grade(meeting_id="M3", rep_email="b@x.com", percentage=90.0, graded_at=now)

    stats = lb._grade_stats_by_rep(start, end)
    assert set(stats) == {"a@x.com", "b@x.com"}
    assert stats["a@x.com"]["calls_graded"] == 2
    assert stats["a@x.com"]["avg_grade_pct"] == 70.0
    assert stats["b@x.com"]["calls_graded"] == 1


def test_grade_stats_counts_critical_misses():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    end = now + timedelta(days=1)
    _insert_grade(meeting_id="M1", rep_email="a@x.com", critical_misses=["x"], graded_at=now)
    _insert_grade(meeting_id="M2", rep_email="a@x.com", critical_misses=[], graded_at=now)
    stats = lb._grade_stats_by_rep(start, end)
    assert stats["a@x.com"]["critical_misses"] == 1


def test_grade_stats_filters_by_call_type():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    end = now + timedelta(days=1)
    _insert_grade(meeting_id="M1", rep_email="s@x.com", call_type="sdr_cold_call", graded_at=now)
    _insert_grade(meeting_id="M2", rep_email="s@x.com", call_type="first_call", graded_at=now)
    stats = lb._grade_stats_by_rep(start, end, call_type_filter=("sdr_cold_call",))
    assert stats["s@x.com"]["calls_graded"] == 1


def test_grade_stats_excludes_rows_outside_window():
    old = datetime.now(timezone.utc) - timedelta(days=30)
    _insert_grade(meeting_id="OLD", rep_email="a@x.com", graded_at=old)
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(days=1)
    stats = lb._grade_stats_by_rep(start, end)
    assert stats == {}


# --------------------------------------------------------------- _pipeline_by_owner

def test_pipeline_by_owner_sums_created_and_won():
    fake = {"records": [
        {"Owner": {"Email": "ae@x.com", "Name": "AE"},
         "Amount": 10000, "CreatedDate": "2026-04-08T12:00:00Z",
         "CloseDate": "2026-04-10", "IsClosed": True, "IsWon": True,
         "StageName": "Closed Won"},
        {"Owner": {"Email": "ae@x.com", "Name": "AE"},
         "Amount": 5000, "CreatedDate": "2026-04-09T12:00:00Z",
         "CloseDate": "2026-06-01", "IsClosed": False, "IsWon": False,
         "StageName": "Proposal"},
    ]}
    start = datetime(2026, 4, 6, tzinfo=timezone.utc)
    end = datetime(2026, 4, 13, tzinfo=timezone.utc)
    with patch.object(lb.salesforce_mcp, "soql_query", return_value=fake):
        out = lb._pipeline_by_owner(start, end)
    assert out["ae@x.com"]["pipeline_created"] == 15000.0
    assert out["ae@x.com"]["closed_won"] == 10000.0


def test_pipeline_by_owner_skips_missing_email():
    fake = {"records": [{"Owner": {}, "Amount": 100, "CreatedDate": "2026-04-09T12:00:00Z",
                          "CloseDate": "2026-04-10", "IsClosed": False, "IsWon": False}]}
    start = datetime(2026, 4, 6, tzinfo=timezone.utc)
    end = datetime(2026, 4, 13, tzinfo=timezone.utc)
    with patch.object(lb.salesforce_mcp, "soql_query", return_value=fake):
        out = lb._pipeline_by_owner(start, end)
    assert out == {}


def test_pipeline_by_owner_degrades_on_soql_error():
    start = datetime(2026, 4, 6, tzinfo=timezone.utc)
    end = datetime(2026, 4, 13, tzinfo=timezone.utc)
    with patch.object(lb.salesforce_mcp, "soql_query", side_effect=RuntimeError("SF down")):
        out = lb._pipeline_by_owner(start, end)
    assert out == {}


# --------------------------------------------------------------- _sdr_meetings_by_owner

def test_sdr_meetings_counts_demo_subjects():
    fake = {"records": [
        {"Owner": {"Email": "s@x.com", "Name": "SDR"},
         "Subject": "Loop demo with Acme", "CreatedDate": "2026-04-08T12:00:00Z"},
        {"Owner": {"Email": "s@x.com", "Name": "SDR"},
         "Subject": "Internal sync", "CreatedDate": "2026-04-09T12:00:00Z"},
        {"Owner": {"Email": "s@x.com", "Name": "SDR"},
         "Subject": "Discovery with Beta", "CreatedDate": "2026-04-09T12:00:00Z"},
    ]}
    start = datetime(2026, 4, 6, tzinfo=timezone.utc)
    end = datetime(2026, 4, 13, tzinfo=timezone.utc)
    with patch.object(lb.salesforce_mcp, "soql_query", return_value=fake):
        out = lb._sdr_meetings_by_owner(start, end)
    assert out["s@x.com"]["meetings_booked"] == 2


def test_sdr_meetings_degrades_on_soql_error():
    start = datetime(2026, 4, 6, tzinfo=timezone.utc)
    end = datetime(2026, 4, 13, tzinfo=timezone.utc)
    with patch.object(lb.salesforce_mcp, "soql_query", side_effect=RuntimeError("boom")):
        out = lb._sdr_meetings_by_owner(start, end)
    assert out == {}


# --------------------------------------------------------------- row assembly + ranking

def test_ae_rows_sorted_by_closed_won_desc():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    end = now + timedelta(days=1)
    _insert_grade(meeting_id="M_A", rep_email="a@x.com", percentage=70.0, graded_at=now)
    _insert_grade(meeting_id="M_B", rep_email="b@x.com", percentage=95.0, graded_at=now)

    pipeline = {
        "a@x.com": {"pipeline_created": 5000.0, "closed_won": 20000.0, "owner_name": "A"},
        "b@x.com": {"pipeline_created": 10000.0, "closed_won": 0.0, "owner_name": "B"},
    }
    with patch.object(lb, "_pipeline_by_owner", return_value=pipeline):
        rows = lb._ae_rows(start, end)
    assert rows[0].rep_email == "a@x.com"
    assert rows[1].rep_email == "b@x.com"


def test_sdr_rows_sorted_by_meetings_booked_desc():
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    end = now + timedelta(days=1)
    _insert_grade(
        meeting_id="M_S1", rep_email="s1@x.com",
        call_type="sdr_cold_call", percentage=85.0, graded_at=now,
    )
    meetings = {
        "s1@x.com": {"meetings_booked": 2, "owner_name": "S1"},
        "s2@x.com": {"meetings_booked": 5, "owner_name": "S2"},
    }
    with patch.object(lb, "_sdr_meetings_by_owner", return_value=meetings):
        rows = lb._sdr_rows(start, end)
    assert rows[0].rep_email == "s2@x.com"
    assert rows[0].meetings_booked == 5
    assert rows[1].rep_email == "s1@x.com"


def test_ae_rows_union_of_grades_and_pipeline():
    """Reps with pipeline-only (no calls) still appear."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=1)
    end = now + timedelta(days=1)
    pipeline = {
        "c@x.com": {"pipeline_created": 100.0, "closed_won": 50.0, "owner_name": "C"},
    }
    with patch.object(lb, "_pipeline_by_owner", return_value=pipeline):
        rows = lb._ae_rows(start, end)
    assert len(rows) == 1
    assert rows[0].calls_graded == 0


# --------------------------------------------------------------- rendering

def test_render_empty_shows_no_activity():
    out = lb._render("ae", "2026-W15", [])
    assert "no activity" in out.lower()


def test_render_ae_includes_money_formatting():
    row = lb.LeaderRow(
        rep_email="ae@x.com", rep_name="AE One",
        calls_graded=3, avg_grade_pct=82.0, critical_misses=0,
        pipeline_created=10000.0, closed_won=25000.0,
    )
    out = lb._render("ae", "2026-W15", [row])
    assert "AE One" in out
    assert "$25,000" in out
    assert "$10,000" in out
    assert "82%" in out


def test_render_sdr_shows_meetings_booked():
    row = lb.LeaderRow(
        rep_email="s@x.com", rep_name="SDR",
        calls_graded=4, avg_grade_pct=78.0, critical_misses=0,
        meetings_booked=6,
    )
    out = lb._render("sdr", "2026-W15", [row])
    assert "6" in out  # meetings booked
    assert "78%" in out


def test_render_flags_critical_misses():
    row = lb.LeaderRow(
        rep_email="x@x.com", rep_name="X",
        calls_graded=3, avg_grade_pct=55.0, critical_misses=2,
        pipeline_created=0.0, closed_won=0.0,
    )
    out = lb._render("ae", "2026-W15", [row])
    assert "2 crit" in out


# --------------------------------------------------------------- snapshot

def test_snapshot_bad_kind_returns_usage():
    out = asyncio.run(lb.snapshot(kind="nope"))
    assert out["error"] == "bad_kind"
    assert "Usage" in out["text"]


def test_snapshot_bad_week_returns_error():
    out = asyncio.run(lb.snapshot(kind="ae", week="not-a-week"))
    assert out["error"] == "bad_week"


def test_snapshot_ae_happy_path():
    with patch.object(lb, "_pipeline_by_owner", return_value={}), \
         patch.object(lb, "_grade_stats_by_rep", return_value={}):
        out = asyncio.run(lb.snapshot(kind="ae"))
    assert out["kind"] == "ae"
    assert out["rows"] == []
    assert "no activity" in out["text"].lower()


def test_snapshot_sdr_includes_week_label():
    with patch.object(lb, "_sdr_meetings_by_owner", return_value={}), \
         patch.object(lb, "_grade_stats_by_rep", return_value={}):
        out = asyncio.run(lb.snapshot(kind="sdr", week="2026-W15"))
    assert out["kind"] == "sdr"
    assert out["week"] == "2026-W15"
