"""SLT Metrics entry point.

OO imports `register_with_dispatcher` at bootstrap to wire us into Slack.
Registers three aliases so `@oo slt …`, `@oo slt-metrics …`, and the
underscore canonical form all route to the same handler.
"""
from __future__ import annotations

from typing import Any

from agents.slt_metrics.agent import SltMetricsAgent
from shared.slack_dispatcher import register


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Slack/dispatcher entry point. One agent instance per call; state lives in DB."""
    return await SltMetricsAgent().run(trigger="slack", payload=payload)


def register_with_dispatcher() -> None:
    """Register all three aliases. Idempotent (dict overwrite is a no-op)."""
    register("slt_metrics", handle)
    register("slt-metrics", handle)
    register("slt", handle)  # scoping doc uses `@oo slt …` as the primary form
