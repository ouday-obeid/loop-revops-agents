import asyncio

from sqlalchemy import text

from shared.agent_base import AgentBase
from shared.db.connection import get_engine


class DemoAgent(AgentBase):
    async def handle(self, trigger, payload):
        return {"echo": payload.get("x")}


def test_run_persists_agent_run():
    agent = DemoAgent(name="demo")
    result = asyncio.run(agent.run("unit", {"x": 1}))
    assert result == {"echo": 1}
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT status, agent_name FROM agent_runs WHERE agent_name='demo' ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert row is not None
    assert row[0] == "success"


def test_run_records_error():
    class Bad(AgentBase):
        async def handle(self, trigger, payload):
            raise ValueError("boom")
    try:
        asyncio.run(Bad(name="bad").run("unit", {}))
    except ValueError:
        pass
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT status, error_message FROM agent_runs WHERE agent_name='bad' ORDER BY id DESC LIMIT 1")
        ).fetchone()
    assert row[0] == "error"
    assert "boom" in row[1]


def test_agent_base_init_with_mcp_dict():
    sf_stub, ff_stub, kn_stub, sl_stub = object(), object(), object(), object()
    agent = DemoAgent(
        name="demo_mcp",
        mcp={"sf": sf_stub, "fireflies": ff_stub, "knowledge": kn_stub, "slack": sl_stub},
    )
    assert agent.sf is sf_stub
    assert agent.fireflies is ff_stub
    assert agent.knowledge is kn_stub
    assert agent.slack is sl_stub


def test_agent_base_attach_still_works_after_mcp_init():
    first = object()
    replacement = object()
    agent = DemoAgent(name="demo_attach", mcp={"sf": first})
    assert agent.sf is first
    agent.attach(sf=replacement, fireflies=object())
    assert agent.sf is replacement
    assert agent.fireflies is not None
