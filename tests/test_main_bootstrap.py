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
