"""M0 — CS dispatcher routing tests."""
from __future__ import annotations

import pytest

from agents.cs.dispatcher import CSDispatcher, HELP_TEXT


@pytest.mark.asyncio
async def test_ping_returns_pong(cs_payload):
    result = await CSDispatcher().handle("slack", cs_payload(text="ping"))
    assert "pong" in result["text"].lower()
    assert "cs online" in result["text"].lower()


@pytest.mark.asyncio
async def test_empty_text_returns_pong(cs_payload):
    result = await CSDispatcher().handle("slack", cs_payload(text=""))
    assert "pong" in result["text"].lower()


@pytest.mark.asyncio
async def test_help_returns_command_list(cs_payload):
    result = await CSDispatcher().handle("slack", cs_payload(text="help"))
    assert result["text"] == HELP_TEXT


@pytest.mark.asyncio
async def test_help_long_flag_routes_to_help(cs_payload):
    result = await CSDispatcher().handle("slack", cs_payload(text="--help"))
    assert result["text"] == HELP_TEXT


@pytest.mark.asyncio
async def test_help_short_flag_routes_to_help(cs_payload):
    result = await CSDispatcher().handle("slack", cs_payload(text="-h"))
    assert result["text"] == HELP_TEXT


@pytest.mark.asyncio
async def test_unknown_command_shows_help(cs_payload):
    result = await CSDispatcher().handle("slack", cs_payload(text="frobnicate"))
    assert "unknown cs command" in result["text"].lower()
    assert "frobnicate" in result["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "status acme", "health acme", "renewals", "churn-risk", "churn-risk 70",
    "brief acme", "qbr acme",
])
async def test_wired_subcommands_never_return_stub(cs_payload, command):
    """All M9-wired subcommands must return real content, not 'not yet wired'."""
    result = await CSDispatcher().handle("slack", cs_payload(text=command))
    assert "not yet wired" not in result["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["status", "health", "brief", "qbr"])
async def test_account_subcommands_require_account_arg(cs_payload, command):
    result = await CSDispatcher().handle("slack", cs_payload(text=command))
    assert "usage" in result["text"].lower()


@pytest.mark.asyncio
async def test_churn_risk_alias_routes(cs_payload):
    """`churn`, `churn-risk`, `churn_risk` all route to same handler."""
    for variant in ("churn", "churn-risk", "churn_risk"):
        result = await CSDispatcher().handle("slack", cs_payload(text=variant))
        assert "churn risk" in result["text"].lower()


@pytest.mark.asyncio
async def test_registration_exposes_handler():
    """bootstrap() must register the cs handler on the shared Slack dispatcher."""
    from agents.cs import main as cs_main
    from shared import slack_dispatcher

    cs_main.bootstrap()
    # parse_command should now route `cs ping` to the cs handler
    agent, rest = slack_dispatcher.parse_command("cs ping")
    assert agent == "cs"
    assert rest == "ping"

    # Dispatch should return our pong
    result = await slack_dispatcher.dispatch("cs ping", {"user": "U_TEST", "channel": "C_TEST"})
    assert "pong" in result["text"].lower()


@pytest.mark.asyncio
async def test_persona_alias_supporter_routes_to_cs():
    """`@oo supporter ping` resolves through PERSONA_ALIASES to the cs handler."""
    from agents.cs import main as cs_main
    from shared import slack_dispatcher

    cs_main.bootstrap()
    result = await slack_dispatcher.dispatch(
        "supporter ping", {"user": "U_TEST", "channel": "C_TEST"}
    )
    assert "pong" in result["text"].lower()
    assert "cs online" in result["text"].lower()


@pytest.mark.asyncio
async def test_persona_alias_supporter_matches_canonical_help():
    """Alias and canonical return identical help text."""
    from agents.cs import main as cs_main
    from shared import slack_dispatcher

    cs_main.bootstrap()
    alias = await slack_dispatcher.dispatch(
        "supporter help", {"user": "U_TEST", "channel": "C_TEST"}
    )
    canonical = await slack_dispatcher.dispatch(
        "cs help", {"user": "U_TEST", "channel": "C_TEST"}
    )
    assert alias["text"] == canonical["text"] == HELP_TEXT
    assert "supporter" in alias["text"]
