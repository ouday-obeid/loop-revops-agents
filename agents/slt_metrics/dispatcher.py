"""SLT Metrics dispatcher — routes `@oo slt <subcommand>` to capability modules.

Routing lives here as a flat dict so each capability stays independently
testable and can be stubbed during D1. Real implementations land in sibling
subpackages over D2-D15 per the sequencing plan.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from agents.slt_metrics.agent import SltMetricsAgent


SubHandler = Callable[[SltMetricsAgent, str, dict[str, Any]], Awaitable[dict[str, Any]]]


# -------------------------------------------------------------------- entry point

async def route(agent: SltMetricsAgent, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
    text_in = (payload.get("text") or "").strip()
    if not text_in or text_in.lower() == "ping":
        return {"text": "pong — slt_metrics online."}

    cmd, _, rest = text_in.partition(" ")
    cmd = cmd.lower().replace("-", "_")

    handler = _SUBCOMMANDS.get(cmd)
    if handler is None:
        return {"text": _help_text(unknown=cmd)}
    return await handler(agent, rest.strip(), payload)


# -------------------------------------------------------------------- subcommand stubs
# Each returns {"stub": True, ...} so Phase 1 tests can assert routing without
# needing the capability modules to exist. They'll be swapped for real imports
# over D2-D14 without changing the routing table.

async def _cmd_forecast(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not args:
        return {"text": "Usage: `@oo slt forecast <quarter>` (e.g. `FY2026-Q2` or `this_quarter`)"}
    return {"stub": True, "cmd": "forecast", "quarter": args, "text": f"forecast stub — quarter={args}"}


async def _cmd_movers(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    period = args or "yesterday"
    return {"stub": True, "cmd": "movers", "period": period, "text": f"movers stub — period={period}"}


async def _cmd_scorecard(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not args:
        return {
            "text": "Usage: `@oo slt scorecard <scope>` "
                    "(e.g. `ae rep@tryloop.ai`, `sdr sdr@tryloop.ai`, `team nate`)"
        }
    parts = args.split(maxsplit=1)
    scope = parts[0].lower()
    target = parts[1].strip() if len(parts) > 1 else ""
    if scope not in ("ae", "sdr", "team"):
        return {"text": f"Unknown scorecard scope `{scope}`. Use `ae`, `sdr`, or `team`."}
    return {
        "stub": True, "cmd": "scorecard",
        "scope": scope, "target": target,
        "text": f"scorecard stub — scope={scope} target={target or '(none)'}",
    }


async def _cmd_briefing(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"stub": True, "cmd": "briefing", "text": "briefing stub — daily 8:30 composer wires in D14"}


async def _cmd_friday(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"stub": True, "cmd": "friday", "text": "friday stub — weekly review composer wires in D14"}


async def _cmd_weights(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    parts = args.split()
    action = (parts[0].lower() if parts else "show")
    if action not in ("show", "set", "propose"):
        return {"text": "Usage: `@oo slt weights [show|set|propose] [pillar=value]`"}
    return {
        "stub": True, "cmd": "weights",
        "action": action, "args": parts[1:],
        "text": f"weights stub — action={action} args={parts[1:]}",
    }


async def _cmd_backtest(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    parts = args.split()
    if len(parts) != 2:
        return {"text": "Usage: `@oo slt backtest <from> <to>` (YYYY-MM-DD)"}
    return {
        "stub": True, "cmd": "backtest",
        "from": parts[0], "to": parts[1],
        "text": f"backtest stub — from={parts[0]} to={parts[1]}",
    }


async def _cmd_help(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"text": _help_text()}


# -------------------------------------------------------------------- routing table

_SUBCOMMANDS: dict[str, SubHandler] = {
    "forecast": _cmd_forecast,
    "movers": _cmd_movers,
    "scorecard": _cmd_scorecard,
    "briefing": _cmd_briefing,
    "friday": _cmd_friday,
    "weights": _cmd_weights,
    "backtest": _cmd_backtest,
    "help": _cmd_help,
}


def _help_text(unknown: str | None = None) -> str:
    header = (
        f"Unknown command `{unknown}`. " if unknown
        else "slt_metrics — SLT revenue intelligence. "
    )
    return header + (
        "Available:\n"
        "• `ping` — health check\n"
        "• `forecast <quarter>` — commit / best-case / weighted rollup\n"
        "• `movers [period]` — top deal movers over a window\n"
        "• `scorecard ae|sdr|team <target>` — per-rep or team scorecard\n"
        "• `briefing` — daily 8:30 AM narrative (DM draft to O)\n"
        "• `friday` — Friday 4 PM weekly review (DM draft to O)\n"
        "• `weights show|set|propose` — inspect / adjust forecast weights\n"
        "• `backtest <from> <to>` — replay scorer over a date range\n"
    )
