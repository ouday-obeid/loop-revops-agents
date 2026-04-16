"""SLT Metrics dispatcher — routes `@oo slt <subcommand>` to capability modules.

Routing lives here as a flat dict so each capability stays independently
testable and can be stubbed during D1. Real implementations land in sibling
subpackages over D2-D15 per the sequencing plan.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from agents.slt_metrics.agent import SltMetricsAgent


log = logging.getLogger(__name__)

SubHandler = Callable[[SltMetricsAgent, str, dict[str, Any]], Awaitable[dict[str, Any]]]


# -------------------------------------------------------------------- entry point

async def route(agent: SltMetricsAgent, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
    text_in = (payload.get("text") or "").strip()
    if not text_in or text_in.lower() == "ping":
        return {"text": "pong — slt_metrics online."}

    cmd, _, rest = text_in.partition(" ")
    cmd_lower = cmd.lower()
    if cmd_lower in ("--help", "-h"):
        cmd = "help"
    else:
        cmd = cmd_lower.replace("-", "_")

    handler = _SUBCOMMANDS.get(cmd)
    if handler is None:
        return {"text": _help_text(unknown=cmd)}
    return await handler(agent, rest.strip(), payload)


# -------------------------------------------------------------------- subcommand stubs
# Each returns {"stub": True, ...} so Phase 1 tests can assert routing without
# needing the capability modules to exist. They'll be swapped for real imports
# over D2-D14 without changing the routing table.

# Module-level aliases so tests can monkeypatch the gate creator / DM sender
# without reaching into `jobs.py`. Real code paths go through `jobs.py`'s
# helpers unchanged — we just rebind these symbols at test time.
def _get_gate_creator():
    from agents.slt_metrics.jobs import _create_gate
    return _create_gate


def _get_default_sender():
    from agents.slt_metrics.jobs import _default_sender
    return _default_sender


def _get_o_dm_channel() -> str:
    from agents.slt_metrics.jobs import _O_DM_CHANNEL
    return _O_DM_CHANNEL


async def _cmd_forecast(agent: SltMetricsAgent, args: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Forecast subcommand — compose a draft, create an slt_draft_review gate, DM O.

    Fixes GH issue #2: previously returned an in-channel stub; now follows the
    same DM-to-O pattern the daily briefing and Friday review use (see
    `jobs.py::_run_briefing`). The narrative is a first-gen placeholder until
    `forecast/narrative.py` grows full Claude-narrated composition — the
    routing contract (`slt_draft_review` gate + DM to O + short confirmation
    in-channel) is what issue #2 is about, and that part is real.
    """
    if not args:
        return {"text": "Usage: `@oo slt forecast <quarter>` (e.g. `FY2026-Q2` or `this_quarter`)"}

    # Lazy import — keeps dispatcher import cheap + avoids circular refs with jobs.py.
    from agents.slt_metrics.forecast import narrative
    from shared.slack_dispatcher import approval_blocks

    try:
        ctx = narrative.build_context(args)
    except narrative.InvalidQuarter as e:
        return {"text": f":warning: {e}"}

    draft = narrative.compose_forecast_draft(ctx)

    create_gate = _get_gate_creator()
    gate_id = create_gate(
        kind=f"forecast:{ctx.quarter.label}",
        run_date=ctx.as_of,
        summary=draft["text"],
    )

    header = f"SLT forecast draft · {ctx.quarter.label}"
    wrapper = approval_blocks(gate_id, header, draft["text"])
    full_blocks = wrapper + draft["blocks"]

    sender = _get_default_sender()
    dm_channel = _get_o_dm_channel()
    send_result = sender(dm_channel, draft["text"], full_blocks)

    log.info(
        "_cmd_forecast: gate=%s quarter=%s deals=%d placeholder=%s",
        gate_id, ctx.quarter.label, ctx.row_count, ctx.placeholder,
    )

    return {
        "cmd": "forecast",
        "quarter": ctx.quarter.label,
        "gate_id": gate_id,
        "dm_channel": dm_channel,
        "slack_ok": bool(send_result.get("ok")) if isinstance(send_result, dict) else False,
        "placeholder": ctx.placeholder,
        "text": f"Forecast draft queued for O review (gate #{gate_id}).",
    }


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
        "Alias: `@oo urkel …` routes here too.\n"
    )
