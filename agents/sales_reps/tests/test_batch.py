"""Batch grader — date-range iteration, skip-already-graded, rate-limit stop."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from agents.sales_reps import rate_gates
from agents.sales_reps.call_grader import batch, storage


def _row(meeting_id: str, title: str = "Intro call") -> dict:
    return {"id": meeting_id, "title": title, "date": "2026-04-10"}


def test_grade_range_empty_list():
    with patch(
        "agents.sales_reps.call_grader.batch.fireflies_adapter.list_recent",
        return_value=[],
    ):
        out = asyncio.run(batch.grade_range("2026-04-01", "2026-04-02"))
    assert out["graded"] == []
    assert out["errors"] == []


def test_grade_range_skips_already_graded():
    storage.upsert_grade({
        "meeting_id": "MTG_B_DUP",
        "rep_email": "rep@tryloop.ai",
        "rep_name": "Rep",
        "call_type": "first_call",
        "scorecard_type": "sc",
        "section_scores": {"x": 3},
        "weighted_total": 3.0,
        "max_weighted": 5.0,
        "percentage": 60.0,
        "pass_fail": "pass_good",
        "evidence": {},
        "feedback": {},
        "strengths": [],
        "improvements": [],
        "critical_misses": [],
        "coaching_summary": "",
        "cell_notes": {},
        "model_used": "claude-sonnet-4-6",
        "tokens_in": 0,
        "tokens_out": 0,
        "cost_usd": 0,
        "transcript_url": None,
        "call_date": None,
    })
    mock_grade = AsyncMock(return_value={"graded": True, "meeting_id": "X"})
    with patch(
        "agents.sales_reps.call_grader.batch.fireflies_adapter.list_recent",
        return_value=[_row("MTG_B_DUP"), _row("MTG_B_NEW")],
    ), patch(
        "agents.sales_reps.call_grader.batch.grader.grade_one", new=mock_grade,
    ):
        out = asyncio.run(batch.grade_range("2026-04-01", "2026-04-02"))
    # Only MTG_B_NEW actually goes through grader.
    assert mock_grade.await_count == 1
    assert "MTG_B_DUP" in out["skipped_already_graded"]


def test_grade_range_counts_graded_vs_skipped():
    mock_grade = AsyncMock()
    mock_grade.side_effect = [
        {"graded": True, "meeting_id": "A", "call_type": "first_call",
         "percentage": 65.0, "grade_label": "pass_good"},
        {"skipped": True, "call_type": "internal", "meeting_id": "B"},
    ]
    with patch(
        "agents.sales_reps.call_grader.batch.fireflies_adapter.list_recent",
        return_value=[_row("MTG_C_A"), _row("MTG_C_B")],
    ), patch(
        "agents.sales_reps.call_grader.batch.grader.grade_one", new=mock_grade,
    ):
        out = asyncio.run(batch.grade_range("2026-04-01", "2026-04-02"))
    assert len(out["graded"]) == 1
    assert len(out["skipped_non_gradable"]) == 1
    assert out["errors"] == []


def test_grade_range_isolates_per_row_errors():
    mock_grade = AsyncMock()
    mock_grade.side_effect = [
        RuntimeError("fireflies boom"),
        {"graded": True, "meeting_id": "B", "call_type": "first_call",
         "percentage": 70.0, "grade_label": "pass_excellent"},
    ]
    with patch(
        "agents.sales_reps.call_grader.batch.fireflies_adapter.list_recent",
        return_value=[_row("MTG_E_A"), _row("MTG_E_B")],
    ), patch(
        "agents.sales_reps.call_grader.batch.grader.grade_one", new=mock_grade,
    ):
        out = asyncio.run(batch.grade_range("2026-04-01", "2026-04-02"))
    assert len(out["errors"]) == 1
    assert out["errors"][0]["meeting_id"] == "MTG_E_A"
    assert "RuntimeError" in out["errors"][0]["error"]
    assert len(out["graded"]) == 1


def test_grade_range_stops_on_rate_limit():
    # First call succeeds; on the second, rate gate raises → loop breaks.
    calls = {"n": 0}

    def fake_check(bucket, window_seconds=3600, *, mode="hard"):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise rate_gates.RateGateExceeded("test limit")
        return calls["n"]

    mock_grade = AsyncMock(return_value={
        "graded": True, "meeting_id": "X", "call_type": "first_call",
        "percentage": 50.0, "grade_label": "pass_good",
    })

    with patch(
        "agents.sales_reps.call_grader.batch.fireflies_adapter.list_recent",
        return_value=[_row("MTG_RL_1"), _row("MTG_RL_2"), _row("MTG_RL_3")],
    ), patch(
        "agents.sales_reps.call_grader.batch.grader.grade_one", new=mock_grade,
    ), patch(
        "agents.sales_reps.call_grader.batch.rate_gates.check", side_effect=fake_check,
    ):
        out = asyncio.run(batch.grade_range("2026-04-01", "2026-04-02"))
    # Only the first row graded; second triggered rate limit.
    assert mock_grade.await_count == 1
    assert out["rate_limited_stopped_at"] == "MTG_RL_2"


def test_grade_range_text_summary_shape():
    mock_grade = AsyncMock(return_value={
        "graded": True, "meeting_id": "X", "call_type": "first_call",
        "percentage": 70.0, "grade_label": "pass_excellent",
    })
    with patch(
        "agents.sales_reps.call_grader.batch.fireflies_adapter.list_recent",
        return_value=[_row("MTG_T_1")],
    ), patch(
        "agents.sales_reps.call_grader.batch.grader.grade_one", new=mock_grade,
    ):
        out = asyncio.run(batch.grade_range("2026-04-01", "2026-04-02"))
    assert "Batch grade complete" in out["text"]
    assert "Graded: 1" in out["text"]
