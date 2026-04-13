"""Onboarding agent bootstrap.

Slack Socket Mode is owned by OO's daemon. This module only needs to register
its dispatcher on the shared registry; OO's daemon will route `@oo onboarding
<cmd>` to it via shared.slack_dispatcher.parse_command.

Call `bootstrap()` once from the OO daemon (or in tests).
"""
from __future__ import annotations

import logging
from typing import Any

from shared.agent_base import AgentBase
from shared.slack_dispatcher import register

from agents.onboarding import dispatcher as onboarding_dispatcher

log = logging.getLogger(__name__)


class OnboardingAgent(AgentBase):
    def __init__(self):
        super().__init__(
            name="onboarding",
            slack_channel="#agent-onboarding-log",
            sf_service_user="revops-agent@tryloop.ai",
            monthly_token_budget=3_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await onboarding_dispatcher.OnboardingDispatcher().handle(trigger, payload)


def bootstrap() -> None:
    """Register the onboarding handler on the shared Slack dispatcher.

    Idempotent — called once by OO's daemon bootstrap, or by tests that
    exercise the registry. Does not start a Slack Socket Mode loop; OO owns
    that.
    """
    register("onboarding", onboarding_dispatcher.handle)
    log.info("onboarding agent registered")
