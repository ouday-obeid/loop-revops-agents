"""SalesRepsAgent — specialist entry point and subcommand router.

Routing lives here; capability modules live under ./call_grader, ./pre_demo,
./pipeline_hygiene.py, ./deal_risk.py, ./momentum_sync_monitor.py,
./leaderboards.py, ./scorecards.py. Router is dumb on purpose: split the text,
look up a method, pass the rest. Keeps each capability independently testable.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from shared.agent_base import AgentBase

log = logging.getLogger(__name__)

# Dept-head Slack user IDs with elevated dispatch access (per scoping §2.2).
# Hutch Fisher — VP Sales — full sales_reps access.
HUTCH_SLACK_USER_ID = "U_HUTCH_PLACEHOLDER"  # set in RUNBOOK on deploy


class SalesRepsAgent(AgentBase):
    """Phase 1 specialist. Read-only SF identity; writes go through approval gates."""

    def __init__(self) -> None:
        super().__init__(
            name="sales_reps",
            slack_channel="#agent-sales-reps-log",
            sf_service_user=None,  # read-only; writes route via revops-agent@ through governance
            monthly_token_budget=8_000_000,  # headroom for call grading volume
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        text_in = (payload.get("text") or "").strip()
        if not text_in or text_in.lower() == "ping":
            return {"text": "pong — sales_reps online."}

        cmd, _, rest = text_in.partition(" ")
        cmd = cmd.lower().replace("-", "_")

        handler = _SUBCOMMANDS.get(cmd)
        if handler is None:
            return {"text": _help_text(unknown=cmd)}
        return await handler(self, rest.strip(), payload)

    # ------------------------------------------------------------------ subcommands

    async def _cmd_grade(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not args:
            return {"text": "Usage: `@oo sales-reps grade <meeting_id>`"}
        from agents.sales_reps.call_grader import grader  # lazy import; keeps ping fast
        return await grader.grade_one(args)

    async def _cmd_batch_grade(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        parts = args.split()
        if len(parts) != 2:
            return {"text": "Usage: `@oo sales-reps batch-grade <from-date> <to-date>` (YYYY-MM-DD)"}
        from agents.sales_reps.call_grader import batch
        return await batch.grade_range(parts[0], parts[1])

    async def _cmd_brief(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not args:
            return {"text": "Usage: `@oo sales-reps brief <opp_id|account_name>`"}
        from agents.sales_reps.pre_demo import brief_generator
        return await brief_generator.generate(args)

    async def _cmd_hygiene(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        from agents.sales_reps import pipeline_hygiene
        ae_filter = args.strip() or None
        return await pipeline_hygiene.run(ae_filter=ae_filter)

    async def _cmd_leaderboard(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        from agents.sales_reps import leaderboards
        parts = args.split()
        kind = (parts[0].lower() if parts else "ae")
        week = parts[1] if len(parts) > 1 else None
        if kind not in ("ae", "sdr"):
            return {"text": "Usage: `@oo sales-reps leaderboard [ae|sdr] [week]`"}
        return await leaderboards.snapshot(kind=kind, week=week)

    async def _cmd_scorecard(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not args:
            return {"text": "Usage: `@oo sales-reps scorecard <rep_email>`"}
        from agents.sales_reps import scorecards
        return await scorecards.for_rep(args.strip())

    async def _cmd_sync_check(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        from agents.sales_reps import momentum_sync_monitor
        return await momentum_sync_monitor.run_once()

    async def _cmd_risk_sweep(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        from agents.sales_reps import deal_risk
        return await deal_risk.run_sweep()

    async def _cmd_help(self, args: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"text": _help_text()}


# -------------------------------------------------------------------- routing table

SubHandler = Callable[[SalesRepsAgent, str, dict[str, Any]], Awaitable[dict[str, Any]]]

_SUBCOMMANDS: dict[str, SubHandler] = {
    "grade": SalesRepsAgent._cmd_grade,
    "batch_grade": SalesRepsAgent._cmd_batch_grade,
    "brief": SalesRepsAgent._cmd_brief,
    "hygiene": SalesRepsAgent._cmd_hygiene,
    "leaderboard": SalesRepsAgent._cmd_leaderboard,
    "scorecard": SalesRepsAgent._cmd_scorecard,
    "sync_check": SalesRepsAgent._cmd_sync_check,
    "risk_sweep": SalesRepsAgent._cmd_risk_sweep,
    "help": SalesRepsAgent._cmd_help,
}


def _help_text(unknown: str | None = None) -> str:
    header = (
        f"Unknown command `{unknown}`. " if unknown
        else "sales_reps — specialist agent. "
    )
    return header + (
        "Available:\n"
        "• `ping` — health check\n"
        "• `grade <meeting_id>` — grade a single call\n"
        "• `batch-grade <from> <to>` — grade calls over a date range (YYYY-MM-DD)\n"
        "• `brief <opp_id|account_name>` — pre-demo brief\n"
        "• `hygiene [ae_email]` — pipeline hygiene report\n"
        "• `leaderboard [ae|sdr] [week]` — weekly rankings\n"
        "• `scorecard <rep_email>` — per-rep scorecard\n"
        "• `sync-check` — Momentum↔SF sync diff\n"
        "• `risk-sweep` — deal-risk signal sweep (pushed close, amount drop, competitor)\n"
    )
