"""SltMetricsAgent — AgentBase subclass; thin glue around the dispatcher.

Read-only Salesforce identity (no sf_service_user). Monthly budget set to the
5M default; forecast detail work (Sonnet daily narrative + Opus mover wrap +
Haiku champion classifier on top-20 opps) is budgeted at ~2.4M tok/mo per plan.
"""
from __future__ import annotations

from typing import Any

from shared.agent_base import AgentBase


class SltMetricsAgent(AgentBase):
    def __init__(self) -> None:
        super().__init__(
            name="slt_metrics",
            slack_channel="#agent-slt-metrics-log",
            sf_service_user=None,  # read-only; never writes to SF
            monthly_token_budget=5_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Lazy import keeps agent construction free of dispatcher side-effects.
        from agents.slt_metrics import dispatcher
        return await dispatcher.route(self, trigger, payload)
