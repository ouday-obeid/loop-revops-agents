"""Top of Funnel entry point.

OO's daemon calls `register_with_dispatcher()` at bootstrap to wire ToF into
Slack. Both `top_of_funnel` (SPECIALISTS-set form) and `tof` (user-facing short
form) route to the same handler.
"""
from __future__ import annotations

from typing import Any

from agents.top_of_funnel.handler import TopOfFunnelAgent
from shared.slack_dispatcher import register


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    """Slack/dispatcher entry point. One agent instance per call; state lives in DB."""
    return await TopOfFunnelAgent().run(trigger="slack", payload=payload)


def register_with_dispatcher() -> None:
    """Register both underscore and short aliases so either form works."""
    register("top_of_funnel", handle)
    register("tof", handle)
