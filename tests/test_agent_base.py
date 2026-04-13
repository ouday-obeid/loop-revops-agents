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
