"""OO Agent daemon entrypoint.

On first boot: runs schema migrations, seeds the initial CEO-tier task,
registers the dispatcher, then starts Slack Socket Mode.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from agents.oo import dispatcher as oo_dispatcher
from agents.cs.main import bootstrap as cs_register
from agents.onboarding.main import register_with_dispatcher as onboarding_register
from agents.revops_support.main import register_with_dispatcher as revops_support_register
from agents.sales_reps.main import register_with_dispatcher as sales_reps_register
from agents.slt_metrics.main import register_with_dispatcher as slt_metrics_register
from agents.top_of_funnel.main import register_with_dispatcher as top_of_funnel_register
from shared.db.connection import get_engine, init_schema
from shared.slack_dispatcher import register, run_socket_mode

log = logging.getLogger(__name__)

SEED_TASK = {
    "agent_name": "revops_support",
    "title": "Add CEO tier to SF role hierarchy",
    "description": (
        "Anand (CEO) is missing from SF role hierarchy — CRO (Henry) is currently top. "
        "Add CEO role above CRO and assign Anand. See scoping doc Appendix B."
    ),
    "priority": "high",
    "category": "sf_reports_missing",
    "source": "manual:scoping_doc",
}


def seed_initial_tasks() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"), {"s": SEED_TASK["source"]}
        ).fetchone()
        if existing:
            return
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee)
                   VALUES (:a, :t, :d, 'pending', :p, :c, :s, 'system')"""
            ),
            {
                "a": SEED_TASK["agent_name"],
                "t": SEED_TASK["title"],
                "d": SEED_TASK["description"],
                "p": SEED_TASK["priority"],
                "c": SEED_TASK["category"],
                "s": SEED_TASK["source"],
            },
        )
        log.info("seeded initial task: %s", SEED_TASK["title"])


def bootstrap() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s: %(message)s")
    init_schema()
    seed_initial_tasks()
    register("oo", oo_dispatcher.handle)
    top_of_funnel_register()
    sales_reps_register()
    onboarding_register()
    cs_register()
    revops_support_register()
    slt_metrics_register()


async def run_daemon() -> None:
    bootstrap()
    log.info("OO daemon starting Slack Socket Mode")
    await run_socket_mode()


if __name__ == "__main__":
    asyncio.run(run_daemon())
