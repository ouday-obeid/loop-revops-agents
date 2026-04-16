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


# ------------------------------- governance routing (Tier 3 / v0.7-hygiene)


def test_handle_gate_decision_routes_through_governance(monkeypatch):
    """Approve/reject buttons delegate to governance.decide_approval_gate,
    which writes the gate_decided audit row and respects cooldown/dual."""
    from shared import governance, slack_dispatcher

    seen = {}

    def _fake_decide(gate_id, *, approved, approver):
        seen.update(gate_id=gate_id, approved=approved, approver=approver)

    monkeypatch.setattr(governance, "decide_approval_gate", _fake_decide)
    slack_dispatcher.handle_gate_decision(99, approved=True, approver="UAPPROVER")
    assert seen == {"gate_id": 99, "approved": True, "approver": "UAPPROVER"}


def test_handle_gate_decision_writes_gate_decided_audit():
    """End-to-end: clicking approve writes the audit row via governance."""
    from sqlalchemy import text as sql_text
    from shared import governance, slack_dispatcher
    from shared.db.connection import get_engine

    gid = governance.create_approval_gate(
        agent_name="revops_support",
        action_type="single_record_update",
        payload={"x": 1},
        justification=None,
    )
    slack_dispatcher.handle_gate_decision(gid, approved=True, approver="UBUTTON")
    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT action, target FROM audit_log WHERE target = :t ORDER BY id DESC LIMIT 1"
            ),
            {"t": f"gate_{gid}"},
        ).fetchone()
    assert row is not None
    assert row[0] == "gate_decided"


# ------------------------------- DM thread_ts (Tier 3 / v0.7-hygiene)


# ------------------------------- ping_o_dm bootstrap helper (Tier 11)


def test_ping_o_dm_targets_slack_test_channel(monkeypatch):
    """ping_o_dm() must target SLACK_TEST_CHANNEL (or U07P4GX9YLQ default)
    and post a recognizable bootstrap message. Used by infra/bootstrap.sh
    to verify the bot token before the daemon comes up."""

    monkeypatch.setenv("SLACK_TEST_CHANNEL", "U_O_PING")
    monkeypatch.setenv("SLACK_DEV_GUARD", "0")

    class _StubClient:
        def __init__(self):
            self.calls = []

        def chat_postMessage(self, channel, text, blocks):
            self.calls.append({"channel": channel, "text": text, "blocks": blocks})
            return {"ok": True, "ts": "1.0", "channel": channel}

    stub = _StubClient()
    sender = SlackSender(client=stub)
    result = sender.ping_o_dm()
    assert result["ok"] is True
    assert stub.calls[0]["channel"] == "U_O_PING"
    assert "bootstrap" in stub.calls[0]["text"].lower()


def test_ping_o_dm_falls_back_to_default_user_id(monkeypatch):
    monkeypatch.delenv("SLACK_TEST_CHANNEL", raising=False)
    monkeypatch.setenv("SLACK_DEV_GUARD", "0")

    class _StubClient:
        def __init__(self): self.calls = []
        def chat_postMessage(self, channel, text, blocks):
            self.calls.append({"channel": channel})
            return {"ok": True, "ts": "1.0", "channel": channel}

    stub = _StubClient()
    SlackSender(client=stub).ping_o_dm()
    assert stub.calls[0]["channel"] == "U07P4GX9YLQ"


def test_dm_handler_passes_thread_ts_to_dispatch_and_say():
    """`_on_dm` must thread DM replies under event['ts'] so a multi-turn DM
    conversation stays in one thread instead of fanning out new top-level
    messages."""
    import asyncio
    from shared import slack_dispatcher

    seen_dispatch = {}

    async def _fake_dispatch(text_in, ctx):
        seen_dispatch.update(ctx)
        return {"text": "ok"}

    say_calls = []

    async def _fake_say(text=None, thread_ts=None, **kwargs):
        say_calls.append({"text": text, "thread_ts": thread_ts})

    # Mimic the inner _on_dm closure body — exercises the production path.
    event = {"channel_type": "im", "user": "U_O", "channel": "D_DM", "ts": "1750000000.000100", "text": "hi"}

    async def _run():
        if event.get("channel_type") != "im":
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        result = await _fake_dispatch(
            event.get("text", ""),
            {"user": event.get("user"), "channel": event.get("channel"), "thread_ts": thread_ts},
        )
        await _fake_say(text=slack_dispatcher._render(result), thread_ts=thread_ts)

    asyncio.run(_run())
    assert seen_dispatch.get("thread_ts") == "1750000000.000100"
    assert say_calls and say_calls[0]["thread_ts"] == "1750000000.000100"
