"""TopOfFunnelAgent — specialist entry point and subcommand router.

Routing lives here; capability modules live under ./icp_scorer.py, ./suppression.py,
./enrichment/, ./sf_lead_writer.py, ./routing.py, ./daily_briefing.py,
./sequence_enroller.py. Router is dumb on purpose: split the text, look up a
method, pass the rest. Keeps each capability independently testable.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from shared.agent_base import AgentBase

log = logging.getLogger(__name__)


class TopOfFunnelAgent(AgentBase):
    """Phase 1 specialist. Writes via tof-agent@tryloop.ai (see RUNBOOK) and
    are gated through shared.governance approval tiers."""

    def __init__(self) -> None:
        super().__init__(
            name="top_of_funnel",
            slack_channel="#agent-tof-log",
            sf_service_user="tof-agent@tryloop.ai",
            monthly_token_budget=5_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        text_in = (payload.get("text") or "").strip()
        if not text_in or text_in.lower() == "ping":
            return {"text": "pong — top_of_funnel online."}

        cmd, _, rest = text_in.partition(" ")
        cmd_lower = cmd.lower()
        if cmd_lower in ("--help", "-h"):
            cmd = "help"
        else:
            cmd = cmd_lower.replace("-", "_")

        handler = _SUBCOMMANDS.get(cmd)
        if handler is None:
            return {"text": _help_text(unknown=cmd)}
        return await handler(self, rest.strip(), payload)

    # -------------------------------------------------------------- subcommands

    async def _cmd_enrich(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not args:
            return {"text": "Usage: `@oo tof enrich <domain>`"}
        from agents.top_of_funnel.enrichment import pipeline
        return await pipeline.enrich_single(args.strip())

    async def _cmd_score(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not args:
            return {"text": "Usage: `@oo tof score <domain>`"}
        from agents.top_of_funnel import icp_scorer
        return await icp_scorer.score_domain(args.strip())

    async def _cmd_daily(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        dry_run = args.strip().lower() in {"dry-run", "dry_run", "dryrun", "preview"}
        from agents.top_of_funnel import daily_briefing
        if dry_run:
            channel = payload.get("user_id") or payload.get("channel") or ""
            return await daily_briefing.send_dry_run(channel)
        return await daily_briefing.send_daily_briefing()

    async def _cmd_suppress(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        parts = args.split(maxsplit=1)
        if not parts:
            return {"text": "Usage: `@oo tof suppress <email> [reason]`"}
        email = parts[0]
        reason = parts[1] if len(parts) > 1 else "manual"
        from agents.top_of_funnel import suppression
        return await suppression.add_manual(email, reason)

    async def _cmd_queue(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        sub, _, rest = args.partition(" ")
        sub = sub.lower()
        from agents.top_of_funnel import sequence_enroller
        if sub == "status":
            return await sequence_enroller.queue_status()
        if sub == "approve":
            gate_id = rest.strip()
            if not gate_id.isdigit():
                return {"text": "Usage: `@oo tof queue approve <gate_id>`"}
            return await sequence_enroller.approve_queue(int(gate_id))
        return {"text": "Usage: `@oo tof queue status` or `@oo tof queue approve <gate_id>`"}

    async def _cmd_credits(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        from agents.top_of_funnel.enrichment import clay_client
        return await clay_client.credit_status()

    async def _cmd_help(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"text": _help_text()}


# ------------------------------------------------------------------ routing table

SubHandler = Callable[[TopOfFunnelAgent, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_SUBCOMMANDS: dict[str, SubHandler] = {
    "enrich": TopOfFunnelAgent._cmd_enrich,
    "score": TopOfFunnelAgent._cmd_score,
    "daily": TopOfFunnelAgent._cmd_daily,
    "suppress": TopOfFunnelAgent._cmd_suppress,
    "queue": TopOfFunnelAgent._cmd_queue,
    "credits": TopOfFunnelAgent._cmd_credits,
    "help": TopOfFunnelAgent._cmd_help,
}


def _help_text(unknown: str | None = None) -> str:
    header = (
        f"Unknown command `{unknown}`. " if unknown
        else "top_of_funnel — Phase 1 specialist agent. "
    )
    return header + (
        "Available:\n"
        "• `ping` — health check\n"
        "• `enrich <domain>` — run the full pipeline on one account\n"
        "• `score <domain>` — ICP score only, no writes\n"
        "• `daily [dry-run]` — trigger or preview the 07:55 SDR briefing\n"
        "• `suppress <email> [reason]` — add to local suppression cache\n"
        "• `queue status` — pending outbound-sequence approval gates\n"
        "• `queue approve <gate_id>` — approve today's enrollment queue\n"
        "• `credits` — Clay credit usage this month\n"
    )
