"""Cover slack_dispatcher's pure-Python paths (no Bolt runtime)."""
import asyncio

from sqlalchemy import text

from shared import slack_dispatcher
from shared.db.connection import get_engine


def test_dispatch_no_handler():
    # Unregister oo if present to force the no-handler path
    slack_dispatcher._registry.pop("oo", None)
    result = asyncio.run(slack_dispatcher.dispatch("nothing here", {}))
    assert "error" in result


def test_dispatch_registered_handler():
    async def dummy(payload): return {"text": "hi"}
    slack_dispatcher.register("dummy_agent", dummy)
    result = asyncio.run(slack_dispatcher.dispatch("dummy_agent go", {}))
    assert result == {"text": "hi"}


def test_handle_gate_decision_updates_db():
    from shared import governance
    gid = governance.create_approval_gate(
        agent_name="t", action_type="single_record_update", payload={}, justification=None
    )
    slack_dispatcher.handle_gate_decision(gid, True, "USLACK")
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT status, approved_by FROM approval_gates WHERE id = :i"), {"i": gid}
        ).fetchone()
    assert row[0] == "approved"
    assert row[1] == "USLACK"


def test_render_various():
    assert slack_dispatcher._render("hello") == "hello"
    assert slack_dispatcher._render({"text": "hi"}) == "hi"
    out = slack_dispatcher._render({"a": 1})
    assert "```" in out


def test_parse_command_bare():
    agent, rest = slack_dispatcher.parse_command("<@U1> oo ping")
    assert agent is None
    assert "ping" in rest


def test_approval_blocks_content():
    blocks = slack_dispatcher.approval_blocks(7, "bulk_update_large", "summary text")
    assert blocks[0]["type"] == "header"
    assert "summary text" in str(blocks)
