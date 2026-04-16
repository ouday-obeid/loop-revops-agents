"""CS dispatcher — routes @oo cs <subcommand> to the appropriate handler.

Subcommands (planned; M0 ships only `ping`):
  ping                     — health check
  status <account>         — summary: health, renewal, cases, last touch
  health <account>         — Vitally score + 30d trend + NPS
  renewals                 — T-120 window + stall list
  churn-risk [tier]        — scored accounts, optional tier filter (50/70/85)
  brief <account>          — on-demand pre-renewal markdown brief
  qbr <account>            — QBR markdown
"""
from __future__ import annotations

from typing import Any

from shared.agent_base import AgentBase

HELP_TEXT = (
    "CS agent commands:\n"
    "• `@oo cs ping` — health check\n"
    "• `@oo cs status <account>` — account summary\n"
    "• `@oo cs health <account>` — Vitally health + trend\n"
    "• `@oo cs renewals` — T-120 window + stalls\n"
    "• `@oo cs churn-risk [50|70|85]` — scored accounts\n"
    "• `@oo cs brief <account>` — pre-renewal brief\n"
    "• `@oo cs qbr <account>` — QBR markdown\n"
    "Alias: `@oo supporter …` routes here too."
)


class CSDispatcher(AgentBase):
    def __init__(self):
        super().__init__(
            name="cs",
            slack_channel="#agent-cs-log",
            monthly_token_budget=6_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        text_in = (payload.get("text") or "").strip()
        if not text_in or text_in.lower() == "ping":
            return {"text": "pong — CS online."}

        parts = text_in.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("--help", "-h"):
            cmd = "help"

        if cmd == "help":
            return {"text": HELP_TEXT}
        if cmd == "status":
            return await self._status(rest)
        if cmd == "health":
            return await self._health(rest)
        if cmd == "renewals":
            return await self._renewals(rest)
        if cmd in ("churn-risk", "churn_risk", "churn"):
            return await self._churn_risk(rest)
        if cmd == "brief":
            return await self._brief(rest)
        if cmd == "qbr":
            return await self._qbr(rest)

        return {"text": f"Unknown cs command: `{cmd}`.\n{HELP_TEXT}"}

    async def _status(self, account: str) -> dict[str, Any]:
        if not account:
            return {"text": "Usage: `@oo cs status <account>`"}
        from agents.cs.queries import account_status
        return {"text": account_status(account)}

    async def _health(self, account: str) -> dict[str, Any]:
        if not account:
            return {"text": "Usage: `@oo cs health <account>`"}
        from agents.cs.queries import account_health_trend
        return {"text": account_health_trend(account)}

    async def _renewals(self, _rest: str) -> dict[str, Any]:
        from agents.cs.queries import renewals_overview
        return {"text": renewals_overview()}

    async def _churn_risk(self, rest: str) -> dict[str, Any]:
        from agents.cs.queries import churn_risk_list
        tier = None
        if rest.strip():
            try:
                tier = int(rest.strip().split()[0])
            except ValueError:
                tier = None
        return {"text": churn_risk_list(tier=tier)}

    async def _brief(self, account: str) -> dict[str, Any]:
        if not account:
            return {"text": "Usage: `@oo cs brief <account>`"}
        from agents.cs.renewal import brief
        return {"text": brief.generate(account)}

    async def _qbr(self, account: str) -> dict[str, Any]:
        if not account:
            return {"text": "Usage: `@oo cs qbr <account>`"}
        from agents.cs.qbr import generator
        return {"text": generator.generate(account)}


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    return await CSDispatcher().run(trigger="slack", payload=payload)
