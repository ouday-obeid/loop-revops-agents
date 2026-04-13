import asyncio

from agents.oo.dispatcher import OODispatcher
from agents.oo.main import seed_initial_tasks


def test_ping():
    out = asyncio.run(OODispatcher().run("test", {"text": "ping"}))
    assert "pong" in out["text"].lower()


def test_board_summary_with_seeded_task():
    seed_initial_tasks()
    out = asyncio.run(OODispatcher().run("test", {"text": "what's on my board?"}))
    assert "CEO tier" in out["text"]


def test_specialist_stub():
    out = asyncio.run(OODispatcher().run("test", {"text": "top_of_funnel do something"}))
    assert "not yet deployed" in out["text"].lower()
