"""Fireflies adapter — normalization, attendee splitting, rep ID, truncation."""
from __future__ import annotations

from unittest.mock import patch

from agents.sales_reps.call_grader import fireflies_adapter as fa


# --------------------------------------------------------------------- helpers

def _raw(
    *,
    meeting_id: str = "MTG_1",
    title: str = "Intro call with Pizzeria X",
    host_email: str = "rep@tryloop.ai",
    participants: list[str] | None = None,
    sentences: list[dict] | None = None,
    duration: int = 45,
) -> dict:
    return {
        "id": meeting_id,
        "title": title,
        "date": "2026-04-10",
        "duration": duration,
        "host_email": host_email,
        "participants": participants or ["rep@tryloop.ai", "buyer@pizzeria.com"],
        "sentences": sentences or [
            {"speaker_name": "Jane Rep", "text": "Hi, this is Jane.", "start_time": 0},
            {"speaker_name": "Buyer Bob", "text": "Hey Jane — good to meet.", "start_time": 5},
        ],
        "summary": {"overview": "intro call"},
        "transcript_url": "https://app.fireflies.ai/view/MTG_1",
    }


# --------------------------------------------------------------------- split

def test_attendee_split_by_domain():
    internal, external = fa._split_attendees([
        "rep@tryloop.ai", "buyer@pizzeria.com", "ae@tryloop.ai", "  ",
    ])
    assert internal == ["rep@tryloop.ai", "ae@tryloop.ai"]
    assert external == ["buyer@pizzeria.com"]


def test_attendee_split_ignores_blanks():
    internal, external = fa._split_attendees(["", None, "rep@tryloop.ai"])  # type: ignore
    assert internal == ["rep@tryloop.ai"]
    assert external == []


def test_attendee_split_lowercases():
    internal, external = fa._split_attendees(["Rep@TRYLOOP.ai", "BUYER@X.com"])
    assert internal == ["rep@tryloop.ai"]
    assert external == ["buyer@x.com"]


# --------------------------------------------------------------------- rep pick

def test_pick_rep_prefers_host_when_internal():
    assert fa._pick_rep("rep@tryloop.ai", ["rep@tryloop.ai", "other@tryloop.ai"]) == "rep@tryloop.ai"


def test_pick_rep_falls_back_to_first_internal_when_host_external():
    assert fa._pick_rep("buyer@pizzeria.com", ["rep@tryloop.ai"]) == "rep@tryloop.ai"


def test_pick_rep_returns_host_when_no_internal():
    assert fa._pick_rep("someone@external.com", []) == "someone@external.com"


def test_pick_rep_returns_none_when_all_missing():
    assert fa._pick_rep(None, []) is None


# --------------------------------------------------------------------- normalize

def test_normalize_splits_and_picks_rep():
    t = fa.normalize(_raw())
    assert t.meeting_id == "MTG_1"
    assert t.rep_email == "rep@tryloop.ai"
    assert t.internal_attendees == ["rep@tryloop.ai"]
    assert t.external_attendees == ["buyer@pizzeria.com"]
    assert t.has_external_attendees is True


def test_normalize_derives_rep_name_from_sentences():
    t = fa.normalize(_raw(
        host_email="jrep@tryloop.ai",
        participants=["jrep@tryloop.ai", "ext@x.com"],
        sentences=[{"speaker_name": "Jrep Smith", "text": "hi"}],
    ))
    assert t.rep_name == "Jrep Smith"


def test_normalize_handles_seconds_duration():
    # 1800 seconds → 30 min
    t = fa.normalize(_raw(duration=1800))
    assert t.duration_minutes == 30.0


def test_normalize_handles_minutes_duration():
    # 30 (already minutes) stays 30
    t = fa.normalize(_raw(duration=30))
    assert t.duration_minutes == 30.0


def test_normalize_internal_only_flags_no_external():
    t = fa.normalize(_raw(
        participants=["rep@tryloop.ai", "other@tryloop.ai"],
    ))
    assert t.has_external_attendees is False


def test_normalize_participant_string_accepted():
    raw = _raw()
    raw["participants"] = "rep@tryloop.ai, buyer@pizzeria.com"
    t = fa.normalize(raw)
    assert t.internal_attendees == ["rep@tryloop.ai"]
    assert t.external_attendees == ["buyer@pizzeria.com"]


# --------------------------------------------------------------------- rendering

def test_rendered_text_joins_sentences():
    t = fa.normalize(_raw())
    out = t.rendered_text()
    assert "Jane Rep: Hi, this is Jane." in out
    assert "Buyer Bob: Hey Jane — good to meet." in out


def test_rendered_text_truncates_when_over_budget():
    # Build 5000 sentences of ~200 chars each → 1M chars, well over 200K budget.
    sentences = [
        {"speaker_name": "S", "text": "x" * 200}
        for _ in range(5000)
    ]
    t = fa.normalize(_raw(sentences=sentences))
    out = t.rendered_text(max_chars=10_000)
    assert "[... transcript truncated for length ...]" in out
    assert len(out) <= 10_000 + 200  # budget + marker


# --------------------------------------------------------------------- fetch

def test_fetch_and_normalize_calls_mcp():
    with patch(
        "agents.sales_reps.call_grader.fireflies_adapter.fireflies_mcp.get_transcript",
        return_value=_raw(),
    ):
        t = fa.fetch_and_normalize("MTG_1")
    assert t.meeting_id == "MTG_1"


def test_fetch_and_normalize_raises_when_empty():
    with patch(
        "agents.sales_reps.call_grader.fireflies_adapter.fireflies_mcp.get_transcript",
        return_value=None,
    ):
        try:
            fa.fetch_and_normalize("MTG_1")
        except ValueError:
            return
    raise AssertionError("expected ValueError")


def test_list_recent_passes_through():
    with patch(
        "agents.sales_reps.call_grader.fireflies_adapter.fireflies_mcp.list_transcripts",
        return_value=[{"id": "a"}, {"id": "b"}],
    ):
        rows = fa.list_recent(from_date="2026-04-01", to_date="2026-04-10")
    assert len(rows) == 2
