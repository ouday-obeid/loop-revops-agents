"""Classifier — rule cascade + Haiku fallback (mocked)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agents.sales_reps.call_grader import classifier
from agents.sales_reps.call_grader.fireflies_adapter import NormalizedTranscript


# --------------------------------------------------------------------- helpers

def _nt(
    *,
    title: str = "",
    external: list[str] | None = None,
    internal: list[str] | None = None,
    duration: float | None = 30.0,
) -> NormalizedTranscript:
    return NormalizedTranscript(
        meeting_id="MTG",
        title=title,
        date="2026-04-10",
        duration_minutes=duration,
        host_email="rep@tryloop.ai",
        rep_email="rep@tryloop.ai",
        rep_name="Rep Rep",
        internal_attendees=internal if internal is not None else ["rep@tryloop.ai"],
        external_attendees=external if external is not None else ["buyer@x.com"],
        sentences=[{"speaker_name": "R", "text": "hi"}],
    )


# --------------------------------------------------------------------- rule 1: no external

def test_no_external_attendees_is_internal():
    c = classifier.classify(_nt(external=[], title="Some call"), allow_haiku=False)
    assert c.call_type == "internal"
    assert c.confidence >= 0.9
    assert c.is_gradable is False


# --------------------------------------------------------------------- rule 2: title regex

@pytest.mark.parametrize("title,expected", [
    ("Onboarding kickoff with Pizzeria X", "onboarding"),
    ("QBR — Account review", "cs"),
    ("Pilot check with prospect", "pilot"),
    ("Renewal discussion", "renewal"),
    ("Headroom capacity review", "headroom"),
    ("2nd call / deep dive", "second_call"),
    ("Intro / discovery", "first_call"),
    ("Follow-up check-in", "follow_up"),
    ("SDR cold call", "sdr_cold_call"),
    ("Internal standup", "internal"),
])
def test_title_regex_classifies(title: str, expected: str):
    c = classifier.classify(_nt(title=title), allow_haiku=False)
    assert c.call_type == expected


def test_title_regex_first_match_wins():
    # "Internal" appears before "onboarding" in the patterns table
    c = classifier.classify(_nt(title="Internal onboarding sync"), allow_haiku=False)
    assert c.call_type == "internal"


# --------------------------------------------------------------------- rule 3: short duration heuristic

def test_short_duration_single_external_is_sdr():
    c = classifier.classify(_nt(title="", duration=5, external=["x@y.com"]), allow_haiku=False)
    assert c.call_type == "sdr_cold_call"
    assert "short_duration" in c.reason


def test_short_duration_multiple_externals_not_sdr():
    c = classifier.classify(
        _nt(title="", duration=5, external=["a@x.com", "b@x.com"]),
        allow_haiku=False,
    )
    # Must NOT auto-classify as SDR with 2+ externals (would be a meeting, not a cold call).
    assert c.call_type != "sdr_cold_call"


# --------------------------------------------------------------------- rule 4: haiku fallback

def test_rules_inconclusive_haiku_disabled_returns_other():
    c = classifier.classify(
        _nt(title="", duration=30),  # no rule fires
        allow_haiku=False,
    )
    assert c.call_type == "other"
    assert "haiku_disabled" in c.reason


def test_haiku_fallback_invoked_when_rules_inconclusive(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    # Simulate a Haiku response.
    fake_text = MagicMock()
    fake_text.text = '{"call_type": "first_call", "confidence": 0.7, "reason": "demo cues"}'
    fake_resp = MagicMock()
    fake_resp.content = [fake_text]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    with patch("anthropic.Anthropic", return_value=fake_client):
        c = classifier.classify(_nt(title="", duration=30), allow_haiku=True)
    assert c.call_type == "first_call"
    assert 0.69 < c.confidence < 0.71
    assert c.reason.startswith("haiku:")


def test_haiku_no_api_key_falls_back_to_other(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = classifier.classify(_nt(title="", duration=30), allow_haiku=True)
    assert c.call_type == "other"
    assert c.reason == "no_anthropic_key_fallback"


def test_haiku_exception_falls_back_to_other(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("network boom")
    with patch("anthropic.Anthropic", return_value=fake_client):
        c = classifier.classify(_nt(title="", duration=30), allow_haiku=True)
    assert c.call_type == "other"
    assert c.reason.startswith("haiku_error:")


def test_haiku_malformed_json_returns_other(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    fake_text = MagicMock()
    fake_text.text = "not-json-at-all"
    fake_resp = MagicMock()
    fake_resp.content = [fake_text]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp
    with patch("anthropic.Anthropic", return_value=fake_client):
        c = classifier.classify(_nt(title="", duration=30), allow_haiku=True)
    # empty parse → default "other"
    assert c.call_type == "other"


# --------------------------------------------------------------------- is_gradable

def test_gradable_property_for_first_call():
    c = classifier.Classification("first_call", 0.8, "title_match")
    assert c.is_gradable is True


def test_gradable_property_for_internal():
    c = classifier.Classification("internal", 0.95, "no_external")
    assert c.is_gradable is False


# --------------------------------------------------------------------- _extract_json

def test_extract_json_with_fences():
    out = classifier._extract_json('```json\n{"a": 1}\n```')
    assert out == {"a": 1}


def test_extract_json_finds_object_in_prose():
    out = classifier._extract_json('here is the result: {"a": 1} and more text')
    assert out == {"a": 1}


def test_extract_json_no_braces_returns_empty():
    assert classifier._extract_json("no json here") == {}
