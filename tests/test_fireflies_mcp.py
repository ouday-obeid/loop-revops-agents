"""Smoke + unit tests for shared.mcp.fireflies_mcp.

Tier 9 of v0.7-hygiene plan (closes Monday subitem on parent
11736843587: 'Smoke test: list_transcripts returns recent O meetings').

We mock _gql so tests run without a real FIREFLIES_API_KEY and don't
hit the live GraphQL endpoint.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from shared.mcp import fireflies_mcp
from shared.mcp.fireflies_mcp import FirefliesError


def _sample_rows() -> list[dict]:
    return [
        {
            "id": "TR_1",
            "title": "ACME pre-demo prep",
            "date": "2026-04-15T14:00:00Z",
            "duration": 28,
            "host_email": "ouday@tryloop.ai",
            "participants": ["ouday@tryloop.ai", "ceo@acme.com"],
        },
        {
            "id": "TR_2",
            "title": "Hutch / Henry weekly",
            "date": "2026-04-15T16:00:00Z",
            "duration": 30,
            "host_email": "hutch@tryloop.ai",
            "participants": ["hutch@tryloop.ai", "henry@tryloop.ai"],
        },
    ]


# ----------------------------------------------- list_transcripts smoke


def test_list_transcripts_returns_list_of_dicts():
    with patch.object(
        fireflies_mcp, "_gql", return_value={"transcripts": _sample_rows()}
    ):
        rows = fireflies_mcp.list_transcripts(limit=5)
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert {r["id"] for r in rows} == {"TR_1", "TR_2"}
    assert rows[0]["title"] == "ACME pre-demo prep"


def test_list_transcripts_passes_filter_args_to_gql():
    captured = {}

    def _capture(query, variables):
        captured["q"] = query
        captured["vars"] = variables
        return {"transcripts": []}

    with patch.object(fireflies_mcp, "_gql", side_effect=_capture):
        fireflies_mcp.list_transcripts(
            from_date="2026-04-01T00:00:00Z",
            to_date="2026-04-15T00:00:00Z",
            participant_email="ouday@tryloop.ai",
            limit=10,
        )
    assert captured["vars"]["from"] == "2026-04-01T00:00:00Z"
    assert captured["vars"]["to"] == "2026-04-15T00:00:00Z"
    assert captured["vars"]["email"] == "ouday@tryloop.ai"
    assert captured["vars"]["limit"] == 10


def test_list_transcripts_returns_empty_on_empty_response():
    with patch.object(fireflies_mcp, "_gql", return_value={"transcripts": []}):
        assert fireflies_mcp.list_transcripts(limit=1) == []


# ----------------------------------------------- get_transcript / search / summary


def test_get_transcript_unwraps_nested_payload():
    payload = {"transcript": {"id": "TR_X", "title": "Demo", "summary": {}}}
    with patch.object(fireflies_mcp, "_gql", return_value=payload):
        out = fireflies_mcp.get_transcript("TR_X")
    assert out["id"] == "TR_X"


def test_search_transcripts_returns_list():
    with patch.object(
        fireflies_mcp, "_gql", return_value={"transcripts": [{"id": "TR_1", "title": "ACME"}]}
    ):
        out = fireflies_mcp.search_transcripts("ACME", limit=5)
    assert out and out[0]["id"] == "TR_1"


def test_get_meeting_summary_unwraps_transcript():
    payload = {
        "transcript": {
            "id": "TR_X",
            "title": "Demo",
            "summary": {"overview": "...", "action_items": []},
        }
    }
    with patch.object(fireflies_mcp, "_gql", return_value=payload):
        out = fireflies_mcp.get_meeting_summary("TR_X")
    assert "summary" in out
    assert out["summary"]["overview"] == "..."


# ----------------------------------------------- _smoke entrypoint


def test_smoke_prints_count_without_crash(capsys):
    with patch.object(
        fireflies_mcp, "list_transcripts", return_value=[{"id": "x"}, {"id": "y"}, {"id": "z"}]
    ):
        fireflies_mcp._smoke()
    captured = capsys.readouterr()
    assert "fireflies transcripts fetched: 3" in captured.out
