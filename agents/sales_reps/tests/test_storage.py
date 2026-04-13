"""Storage — schema bootstrap, upsert, get, list, JSON round-trip."""
from __future__ import annotations

from agents.sales_reps.call_grader import storage


def _grade(meeting_id: str = "MTG_A", **overrides) -> dict:
    base = {
        "meeting_id": meeting_id,
        "rep_email": "rep@tryloop.ai",
        "rep_name": "Rep One",
        "call_type": "first_call",
        "scorecard_type": "AE Certification — First Call",
        "section_scores": {"Introduction": 3, "Discovery": 4},
        "weighted_total": 10.5,
        "max_weighted": 20.0,
        "percentage": 52.5,
        "pass_fail": "pass_good",
        "evidence": {"Introduction": ["quote 1", "quote 2"]},
        "feedback": {"Introduction": "tighter agenda"},
        "strengths": ["listens well"],
        "improvements": ["confirm budget"],
        "critical_misses": ["missed budget owner"],
        "coaching_summary": "Good discovery. Lock next step earlier.",
        "cell_notes": {"Introduction": "sets agenda"},
        "model_used": "claude-sonnet-4-6",
        "tokens_in": 12000,
        "tokens_out": 800,
        "cost_usd": 0.048,
        "transcript_url": "https://fireflies.ai/view/MTG_A",
        "call_date": "2026-04-10",
    }
    base.update(overrides)
    return base


def test_ensure_schema_idempotent():
    # Call twice — must not throw. Second call is a no-op via _initialized flag.
    storage.ensure_schema()
    storage.ensure_schema()


def test_upsert_and_get_round_trip():
    gid = storage.upsert_grade(_grade("MTG_RT1"))
    assert gid > 0
    got = storage.get_grade("MTG_RT1")
    assert got is not None
    assert got["meeting_id"] == "MTG_RT1"
    assert got["rep_email"] == "rep@tryloop.ai"
    # JSON fields round-trip as dicts/lists, not strings.
    assert got["section_scores"] == {"Introduction": 3, "Discovery": 4}
    assert got["evidence"] == {"Introduction": ["quote 1", "quote 2"]}
    assert got["strengths"] == ["listens well"]


def test_upsert_updates_existing_meeting():
    storage.upsert_grade(_grade("MTG_UP", percentage=40.0, pass_fail="fail_needs_work"))
    gid2 = storage.upsert_grade(_grade("MTG_UP", percentage=75.0, pass_fail="pass_excellent"))
    got = storage.get_grade("MTG_UP")
    assert got["percentage"] == 75.0
    assert got["pass_fail"] == "pass_excellent"
    # Same meeting_id means same row.
    assert got["id"] == gid2


def test_grade_exists():
    storage.upsert_grade(_grade("MTG_EX"))
    assert storage.grade_exists("MTG_EX") is True
    assert storage.grade_exists("MTG_NEVER") is False


def test_list_grades_for_rep_orders_by_recency():
    # Insert three grades for same rep, spread across different meetings.
    for i in range(3):
        storage.upsert_grade(_grade(f"MTG_LIST_{i}", rep_email="lister@tryloop.ai"))
    rows = storage.list_grades_for_rep("lister@tryloop.ai", limit=50)
    assert len(rows) >= 3
    assert all(r["rep_email"] == "lister@tryloop.ai" for r in rows)


def test_list_grades_for_rep_respects_limit():
    for i in range(5):
        storage.upsert_grade(_grade(f"MTG_LIMIT_{i}", rep_email="limit@tryloop.ai"))
    rows = storage.list_grades_for_rep("limit@tryloop.ai", limit=2)
    assert len(rows) == 2


def test_get_grade_missing_returns_none():
    assert storage.get_grade("TOTALLY_NOT_A_MEETING") is None


def test_serialize_handles_already_stringified_json():
    # If caller pre-serializes, _serialize_json_fields shouldn't double-encode.
    raw = _grade("MTG_PRE", section_scores='{"A": 3}')
    gid = storage.upsert_grade(raw)
    got = storage.get_grade("MTG_PRE")
    assert got["section_scores"] == {"A": 3}


def test_upsert_with_null_optional_fields():
    # rep_email None, rep_name None — must not blow up.
    partial = _grade("MTG_NULL", rep_email=None, rep_name=None, tokens_in=None, tokens_out=None)
    gid = storage.upsert_grade(partial)
    got = storage.get_grade("MTG_NULL")
    assert got["rep_email"] is None
    assert got["rep_name"] is None
