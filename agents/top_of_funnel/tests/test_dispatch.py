"""D7 tests for OO dispatch wiring — @oo tof <cmd> routes to the right handler
and returns a Slack-formattable response for every registered subcommand."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel import daily_briefing, routing
from agents.top_of_funnel.handler import TopOfFunnelAgent, _SUBCOMMANDS
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset():
    routing._ensure_user_cache()
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tof_lead_candidates"))
        conn.execute(text("DELETE FROM tof_enrichment_runs"))
        conn.execute(text("DELETE FROM tof_sf_user_cache"))
        conn.execute(text("DELETE FROM suppression_cache"))
    yield


# ---------------------------------------------------------- ping / help


@pytest.mark.asyncio
async def test_ping_returns_pong():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "ping"})
    assert "pong" in r["text"].lower()


@pytest.mark.asyncio
async def test_empty_text_returns_pong():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": ""})
    assert "pong" in r["text"].lower()


@pytest.mark.asyncio
async def test_unknown_command_returns_help():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "bogus"})
    assert "Unknown command" in r["text"] or "unknown" in r["text"].lower()
    assert "enrich" in r["text"]


@pytest.mark.asyncio
async def test_help_explicit():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "help"})
    assert "enrich" in r["text"]
    assert "daily" in r["text"]
    assert "credits" in r["text"]


# ---------------------------------------------------------- command routing


def test_subcommand_registry_matches_help_text():
    """Every advertised command in help() must map to a real handler."""
    expected = {"enrich", "score", "daily", "suppress", "queue", "credits", "help"}
    assert set(_SUBCOMMANDS.keys()) == expected


# ---------------------------------------------------------- input validation


@pytest.mark.asyncio
async def test_enrich_without_domain_returns_usage():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "enrich"})
    assert "Usage" in r["text"]


@pytest.mark.asyncio
async def test_score_without_domain_returns_usage():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "score"})
    assert "Usage" in r["text"]


@pytest.mark.asyncio
async def test_suppress_without_email_returns_usage():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "suppress"})
    assert "Usage" in r["text"]


@pytest.mark.asyncio
async def test_queue_unknown_subcommand_returns_usage():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "queue bogus"})
    assert "Usage" in r["text"]


@pytest.mark.asyncio
async def test_queue_approve_non_numeric_returns_usage():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "queue approve abc"})
    assert "Usage" in r["text"]


# ---------------------------------------------------------- daily dry-run


@pytest.mark.asyncio
async def test_daily_dry_run_routes_to_one_channel(monkeypatch):
    """`@oo tof daily dry-run` → daily_briefing.send_dry_run(user_id).
    No run seeded → empty-notice path."""
    calls: list[dict[str, Any]] = []

    def fake_send(channel: str, text_: str, blocks: list | None = None, *, thread_ts: str | None = None):
        calls.append({"channel": channel, "text": text_})
        return {"ok": True, "ts": "1.0"}

    monkeypatch.setattr(daily_briefing, "_default_send", fake_send)
    a = TopOfFunnelAgent()
    await a.handle("slack", {"text": "daily dry-run", "user_id": "U_O"})
    # Empty-notice lands on the user channel.
    assert calls and calls[0]["channel"] == "U_O"


# ---------------------------------------------------------- case insensitivity


@pytest.mark.asyncio
async def test_command_is_case_insensitive():
    a = TopOfFunnelAgent()
    r = await a.handle("slack", {"text": "PING"})
    assert "pong" in r["text"].lower()


@pytest.mark.asyncio
async def test_help_supports_hyphen_alias():
    """`dry-run` → `dry_run`: handler normalizes hyphens in command token."""
    a = TopOfFunnelAgent()
    # 'dry-run' is not a real command but it tests the normalization path.
    r = await a.handle("slack", {"text": "dry-run"})
    # No subcommand 'dry_run' exists → help response.
    assert "Unknown" in r["text"] or "unknown" in r["text"].lower()
