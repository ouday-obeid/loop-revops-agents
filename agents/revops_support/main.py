"""RevOps Support entry point.

OO imports `register_with_dispatcher` at bootstrap to wire us into Slack. The
dispatcher routes both `revops_support` (SPECIALISTS set convention) and
`revops-support` (user-facing hyphen form) to the same handler.
"""
from __future__ import annotations

from typing import Any

from agents.revops_support.agent import RevOpsSupportAgent
from shared.slack_dispatcher import register


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Slack/dispatcher entry point. One agent instance per call; state lives in DB."""
    return await RevOpsSupportAgent().run(trigger="slack", payload=payload)


def register_with_dispatcher() -> None:
    """Register both underscore and hyphen aliases so either form works."""
    register("revops_support", handle)
    register("revops-support", handle)
