import asyncio
import os

import pytest

from shared import slack_dispatcher
from shared.slack_dispatcher import (
    PERSONA_ALIASES,
    SlackSender,
    approval_blocks,
    parse_command,
)


def test_parse_command_with_agent():
    agent, rest = parse_command("<@U123> unregistered_agent_xyz score leads")
    # falls through when first token is not a registered handler
    assert agent is None
    assert "unregistered_agent_xyz" in rest


def test_parse_command_registered():
    async def dummy(p): return {"ok": True}
    slack_dispatcher.register("foo", dummy)
    agent, rest = parse_command("<@U123> foo bar baz")
    assert agent == "foo"
    assert rest == "bar baz"


def test_approval_blocks_shape():
    blocks = approval_blocks(42, "bulk_update_large", "500 accounts")
    assert any(b.get("type") == "actions" for b in blocks)
    assert blocks[-1]["block_id"] == "gate_42"


# ------------------------------------------------------------- persona aliases


@pytest.fixture
def _registered_canonicals():
    """Register the 6 canonical agent names so persona-alias resolution has
    something to land on. Saves + restores the registry."""
    saved = dict(slack_dispatcher._registry)
    slack_dispatcher._registry.clear()

    async def _dummy(payload):  # pragma: no cover - fixture plumbing
        return {"ok": True}

    for canonical in set(PERSONA_ALIASES.values()):
        slack_dispatcher.register(canonical, _dummy)
    yield
    slack_dispatcher._registry.clear()
    slack_dispatcher._registry.update(saved)


@pytest.mark.parametrize(
    "persona,canonical",
    [
        ("outbounder", "top_of_funnel"),
        ("closer", "sales_reps"),
        ("onboarder", "onboarding"),
        ("supporter", "cs"),
        ("admin", "revops_support"),
        ("urkel", "slt_metrics"),
    ],
)
def test_persona_alias_resolves_to_canonical(
    persona, canonical, _registered_canonicals
):
    """parse_command normalizes human persona names to the canonical registry
    key before routing, so `@oo <persona> <rest>` lands on the same handler as
    `@oo <canonical> <rest>`."""
    agent, rest = parse_command(f"<@U0BOT> {persona} some rest text")
    assert agent == canonical
    assert rest == "some rest text"


def test_dev_guard_redirects_off_target():
    os.environ["SLACK_DEV_GUARD"] = "1"
    os.environ["SLACK_TEST_CHANNEL"] = "UTEST"

    class _StubClient:
        def __init__(self):
            self.calls = []

        def chat_postMessage(self, channel, text, blocks):
            self.calls.append({"channel": channel, "text": text, "blocks": blocks})
            return {"ok": True, "ts": "1.0", "channel": channel}

    stub = _StubClient()
    sender = SlackSender(client=stub)
    result = sender.send("CDIFFERENT", "hello")
    assert result["ok"] is True
    assert stub.calls[0]["channel"] == "UTEST"
    assert "dev-guard" in stub.calls[0]["text"] and "CDIFFERENT" in stub.calls[0]["text"]
    os.environ["SLACK_DEV_GUARD"] = "0"
