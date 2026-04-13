"""CS Agent bootstrap.

The Slack Socket Mode daemon is owned by OO. CS only needs to register its
dispatcher on the shared registry; OO's daemon will route @oo cs <cmd> to it
via shared.slack_dispatcher.parse_command.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.agent_base import AgentBase
from shared.slack_dispatcher import register

from agents.cs import dispatcher as cs_dispatcher

log = logging.getLogger(__name__)


class CSAgent(AgentBase):
    def __init__(self):
        super().__init__(
            name="cs",
            slack_channel="#agent-cs-log",
            monthly_token_budget=6_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await cs_dispatcher.CSDispatcher().handle(trigger, payload)


def bootstrap() -> None:
    """Register CS with the shared Slack dispatcher.

    Called once by OO's daemon bootstrap (or by tests). Idempotent.
    """
    register("cs", cs_dispatcher.handle)
    log.info("cs agent registered")
