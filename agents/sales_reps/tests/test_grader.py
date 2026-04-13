"""Grader end-to-end — fetch → classify → grade → persist → audit. Mocked externals."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from agents.sales_reps.call_grader import classifier, grader, storage
from agents.sales_reps.call_grader.fireflies_adapter import NormalizedTranscript
from shared.db.connection import get_engine


# --------------------------------------------------------------------- helpers

def _nt(meeting_id: str = "MTG_G", title: str = "Intro with Prospect X") -> NormalizedTranscript:
    return NormalizedTranscript(
        meeting_id=meeting_id,
        title=title,
        date="2026-04-10",
        duration_minutes=30.0,
        host_email="ae@tryloop.ai",
        rep_email="ae@tryloop.ai",
        rep_name="AE Rep",
        internal_attendees=["ae@tryloop.ai"],
        external_attendees=["buyer@prospect.com"],
        sentences=[{"speaker_name": "AE Rep", "text": "Hi there — thanks for joining."}],
        transcript_url="https://fireflies.ai/view/MTG_G",
    )


def _sonnet_response(
    call_type: str = "first_call",
    section_scores: dict[str, int] | None = None,
    critical_misses: dict[str, list[str]] | None = None,
) -> MagicMock:
    """Build a mock Anthropic messages.create response."""
    from agents.sales_reps.call_grader import rubrics as r
    rubric = r.get_rubric(call_type)
    scores = section_scores or {s.name: 3 for s in rubric.sections}
    misses_by_section = critical_misses or {}
    sections_out = {}
    for s in rubric.sections:
        sections_out[s.name] = {
            "score": scores.get(s.name, 3),
            "evidence": [f"quote from {s.name}"],
            "feedback": f"feedback for {s.name}",
            "cell_note": f"note for {s.name}",
            "critical_misses": misses_by_section.get(s.name, []),
        }
    payload = {
        "call_type_confirmed": call_type,
        "sections": sections_out,
        "overall_strengths": ["strong opener"],
        "overall_improvements": ["lock next step"],
        "critical_misses": [],
        "coaching_summary": "Solid call. Tighten the close.",
    }
    text_block = MagicMock()
    text_block.text = json.dumps(payload)
    resp = MagicMock()
    resp.content = [text_block]
    resp.usage = MagicMock(input_tokens=10_000, output_tokens=800)
    return resp


# --------------------------------------------------------------------- system prompt

def test_build_system_prompt_contains_all_sections():
    from agents.sales_reps.call_grader import rubrics as r
    rubric = r.get_rubric("first_call")
    prompt = grader._build_system_prompt(rubric)
    for s in rubric.sections:
        assert s.name in prompt
    assert rubric.scorecard_name in prompt
    assert "Return ONLY JSON" in prompt


def test_build_system_prompt_marks_critical_items():
    from agents.sales_reps.call_grader import rubrics as r
    rubric = r.get_rubric("sdr_cold_call")
    prompt = grader._build_system_prompt(rubric)
    assert "[critical:" in prompt


# --------------------------------------------------------------------- extract_json

def test_extract_json_parses_plain_object():
    out = grader._extract_json('{"a": 1, "b": [1,2]}')
    assert out == {"a": 1, "b": [1, 2]}


def test_extract_json_strips_fences():
    out = grader._extract_json('```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_extract_json_returns_empty_for_garbage():
    assert grader._extract_json("no json at all") == {}


def test_extract_json_tolerates_leading_trailing_prose():
    out = grader._extract_json('Here you go: {"a": 1} — done')
    assert out == {"a": 1}


# --------------------------------------------------------------------- grade_one: skip path

def test_grade_one_skips_non_gradable(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    with patch(
        "agents.sales_reps.call_grader.grader.fireflies_adapter.fetch_and_normalize",
        return_value=_nt(title="Internal standup"),  # classifier → internal
    ):
        out = asyncio.run(grader.grade_one("MTG_SKIP", allow_haiku=False))
    assert out["skipped"] is True
    assert out["call_type"] == "internal"


# --------------------------------------------------------------------- grade_one: happy path

def test_grade_one_happy_path_persists_and_audits(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    nt = _nt("MTG_HP", title="Intro / discovery with Pizzeria X")  # → first_call

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _sonnet_response("first_call")

    with patch(
        "agents.sales_reps.call_grader.grader.fireflies_adapter.fetch_and_normalize",
        return_value=nt,
    ), patch("anthropic.Anthropic", return_value=fake_client):
        out = asyncio.run(grader.grade_one("MTG_HP", allow_haiku=False))

    assert out["graded"] is True
    assert out["meeting_id"] == "MTG_HP"
    assert out["call_type"] == "first_call"
    assert 0 <= out["percentage"] <= 100
    assert out["grade_label"] in (
        "pass_excellent", "pass_good", "fail_needs_work", "fail_major_gaps"
    )

    # Persisted row exists.
    saved = storage.get_grade("MTG_HP")
    assert saved is not None
    assert saved["call_type"] == "first_call"
    assert saved["model_used"] == "claude-sonnet-4-6"
    assert saved["tokens_in"] == 10_000
    assert saved["tokens_out"] == 800

    # Audit row exists.
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT action, target FROM audit_log "
                "WHERE agent_name='sales_reps' AND target='fireflies:MTG_HP' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "sales_reps_grade_call"


def test_grade_one_enforces_critical_cap(monkeypatch):
    """If the LLM reports a critical miss but gives a 5, _finalize must cap at 3."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    nt = _nt("MTG_CAP", title="Intro / discovery")  # → first_call

    from agents.sales_reps.call_grader import rubrics as r
    rubric = r.get_rubric("first_call")
    # Every section gets 5, but Discovery has a critical miss → should cap at 3.
    all_five = {s.name: 5 for s in rubric.sections}
    critical = {"Discovery": ["missed location count"]}

    fake_client = MagicMock()
    fake_client.messages.create.return_value = _sonnet_response(
        "first_call", section_scores=all_five, critical_misses=critical
    )

    with patch(
        "agents.sales_reps.call_grader.grader.fireflies_adapter.fetch_and_normalize",
        return_value=nt,
    ), patch("anthropic.Anthropic", return_value=fake_client):
        out = asyncio.run(grader.grade_one("MTG_CAP", allow_haiku=False))

    saved = storage.get_grade("MTG_CAP")
    assert saved["section_scores"]["Discovery"] == 3
    # Other sections keep their 5.
    for name, score in saved["section_scores"].items():
        if name != "Discovery":
            assert score == 5


def test_grade_one_raises_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_SECRET", raising=False)
    nt = _nt("MTG_NOKEY", title="Intro / discovery")
    with patch(
        "agents.sales_reps.call_grader.grader.fireflies_adapter.fetch_and_normalize",
        return_value=nt,
    ):
        with pytest.raises(RuntimeError):
            asyncio.run(grader.grade_one("MTG_NOKEY", allow_haiku=False))


def test_grade_one_malformed_llm_json_still_returns_summary(monkeypatch):
    """Broken JSON from LLM → finalize uses defaults, doesn't crash the batch run."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    nt = _nt("MTG_BADJSON", title="Intro / discovery")

    broken = MagicMock()
    broken.text = "not json at all"
    fake_resp = MagicMock()
    fake_resp.content = [broken]
    fake_resp.usage = MagicMock(input_tokens=100, output_tokens=10)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    with patch(
        "agents.sales_reps.call_grader.grader.fireflies_adapter.fetch_and_normalize",
        return_value=nt,
    ), patch("anthropic.Anthropic", return_value=fake_client):
        out = asyncio.run(grader.grade_one("MTG_BADJSON", allow_haiku=False))

    # All sections default to 1 → percentage = 20%, label = fail_major_gaps.
    assert out["graded"] is True
    assert out["percentage"] == 20.0
    assert out["grade_label"] == "fail_major_gaps"


# --------------------------------------------------------------------- build_user_message

def test_build_user_message_includes_metadata_and_transcript():
    nt = _nt("MTG_UM")
    msg = grader._build_user_message(nt)
    assert "MTG_UM" in msg
    assert "ae@tryloop.ai" in msg
    assert "buyer@prospect.com" in msg
    assert "Hi there" in msg  # rendered transcript line
