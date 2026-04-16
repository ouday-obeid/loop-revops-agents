import asyncio
import time

from sqlalchemy import text

from agents.oo.main import bootstrap, seed_initial_tasks, SEED_TASK
from shared.db.connection import get_engine


def test_bootstrap_idempotent():
    bootstrap()
    bootstrap()  # second call: seed should be idempotent
    with get_engine().begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source = :s"), {"s": SEED_TASK["source"]}
        ).scalar()
    assert count == 1


def test_seed_skips_on_existing():
    seed_initial_tasks()
    seed_initial_tasks()
    with get_engine().begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source = :s"), {"s": SEED_TASK["source"]}
        ).scalar()
    assert count == 1


def test_oo_ping_roundtrip_under_2s():
    """`@oo ping` must round-trip in well under 2 seconds (SLO from
    Monday parent 11736854676). The handler is in-process Python with one
    DB write for agent_runs; comfortably <2s on any laptop, but we assert
    it explicitly so future regressions (e.g. accidental network call
    inserted in the ping path) get caught."""
    from agents.oo.dispatcher import handle as oo_handle

    start = time.monotonic()
    result = asyncio.run(oo_handle({"text": "ping"}))
    elapsed = time.monotonic() - start
    assert "pong" in result.get("text", "").lower()
    assert elapsed < 2.0, f"ping took {elapsed:.3f}s — over 2s SLO"


def test_oo_ping_threading_context_propagates():
    """Verifies the dispatcher entrypoint accepts and ignores the thread_ts
    field that slack_dispatcher._on_dm now injects. Guards against future
    handler changes that strip unrecognized payload keys."""
    from agents.oo.dispatcher import handle as oo_handle

    payload = {"text": "ping", "user": "U_O", "channel": "D_DM", "thread_ts": "1750000000.000100"}
    result = asyncio.run(oo_handle(payload))
    assert "pong" in result.get("text", "").lower()
