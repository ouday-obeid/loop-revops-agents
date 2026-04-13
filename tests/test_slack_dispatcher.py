import asyncio
import os

from shared import slack_dispatcher
from shared.slack_dispatcher import SlackSender, approval_blocks, parse_command


def test_parse_command_with_agent():
    agent, rest = parse_command("<@U123> top_of_funnel score leads")
    # 'top_of_funnel' not registered in test; falls through
    assert agent is None
    assert "top_of_funnel" in rest


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


def test_dev_guard_blocks_off_target():
    os.environ["SLACK_DEV_GUARD"] = "1"
    os.environ["SLACK_TEST_CHANNEL"] = "UTEST"
    sender = SlackSender(client=object())
    result = sender.send("CDIFFERENT", "hello")
    assert result["blocked"] is True
    os.environ["SLACK_DEV_GUARD"] = "0"
