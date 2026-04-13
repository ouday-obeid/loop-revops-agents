"""Sales Reps entry point.

OO imports `register_with_dispatcher` at bootstrap to wire us into Slack. The
dispatcher routes both `sales_reps` (SPECIALISTS set convention) and
`sales-reps` (user-facing hyphen form) to the same handler.
"""
from __future__ import annotations

from typing import Any

from agents.sales_reps.handler import SalesRepsAgent
from shared.slack_dispatcher import register


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Slack/dispatcher entry point. One agent instance per call; state lives in DB."""
    return await SalesRepsAgent().run(trigger="slack", payload=payload)


def register_with_dispatcher() -> None:
    """Register both underscore and hyphen aliases so either form works."""
    register("sales_reps", handle)
    register("sales-reps", handle)
