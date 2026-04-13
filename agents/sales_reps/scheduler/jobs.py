"""Scheduled tick runners — invoked by launchd plists (Phase 1) and Cloud Scheduler (Phase 4).

Each tick is a thin async wrapper around a capability module's public entry point.
Launchd plists shell out to `python -m agents.sales_reps.scheduler.jobs <tick_name>`
so the dispatch table doubles as the CLI contract. Keep the plist rows lean; real
logic lives in the capability modules.

The weekly leaderboard and scorecard ticks are *computed* here but posted via
Hutch-gated approval in Phase 1 — see RUNBOOK.md §"Weekly Hutch gate".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from agents.sales_reps import (
    deal_risk,
    leaderboards,
    momentum_sync_monitor,
    pipeline_hygiene,
    scorecards,
)
from agents.sales_reps.call_grader import batch as grader_batch
from agents.sales_reps.pre_demo import brief_generator, trigger as demo_trigger
from shared import governance

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"


# --------------------------------------------------------------- grader poll

async def grader_poll() -> dict[str, Any]:
    """Every 15 min: grade any new Fireflies transcripts in the last 20 min window.

    Look-back window is slightly wider than tick cadence so a delayed tick still
    catches late arrivals. `grade_range` is idempotent via storage.grade_exists.
    """
    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(minutes=20)).date().isoformat()
    window_end = (now + timedelta(days=1)).date().isoformat()  # inclusive of today
    out = await grader_batch.grade_range(window_start, window_end, limit=50)
    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_grader_poll",
        target=f"window:{window_start}..{window_end}",
        after={"graded": len(out.get("graded", [])),
               "errors": len(out.get("errors", []))},
    )
    return out


# --------------------------------------------------------------- pre-demo brief scan

async def brief_scan() -> dict[str, Any]:
    """Every 15 min: find demos starting in ~2h and generate briefs for each."""
    candidates = demo_trigger.scan_upcoming()
    briefs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for cand in candidates:
        opp = cand.get("opportunity") or {}
        opp_id = opp.get("Id")
        if not opp_id:
            continue
        try:
            brief = await brief_generator.generate(opp_id, include_blocks=True)
            briefs.append({
                "opportunity_id": opp_id,
                "event_title": (cand.get("event") or {}).get("title"),
                "has_text": bool(brief.get("text")),
            })
        except Exception as e:  # noqa: BLE001 — per-candidate isolation
            log.exception("brief_scan: generate failed for %s", opp_id)
            errors.append({"opportunity_id": opp_id, "error": f"{type(e).__name__}: {e}"})

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_brief_scan",
        target=f"briefs:{len(briefs)}",
        after={"briefs": len(briefs), "errors": len(errors),
               "candidates": len(candidates)},
    )
    return {"briefs": briefs, "errors": errors, "candidates_scanned": len(candidates)}


# --------------------------------------------------------------- daily / periodic

async def hygiene_daily() -> dict[str, Any]:
    """Daily 07:00 ET: pipeline hygiene report, no AE filter (org-wide)."""
    return await pipeline_hygiene.run(ae_filter=None)


async def sync_check() -> dict[str, Any]:
    """Every 30 min: Momentum↔SF ActivityHistory diff."""
    return await momentum_sync_monitor.run_once()


async def risk_sweep() -> dict[str, Any]:
    """Every 2h: pushed-close / amount-drop / champion-gone / competitor sweep."""
    return await deal_risk.run_sweep()


# --------------------------------------------------------------- weekly

async def leaderboard_weekly() -> dict[str, Any]:
    """Friday 16:00 ET: compute AE + SDR leaderboards for the current ISO week.

    Phase 1: returned payload is posted to Hutch DM behind an approval gate
    (see RUNBOOK.md §"Weekly Hutch gate"). The tick only *computes* — posting
    happens in the scheduler-side caller which this job returns to.
    """
    ae = await leaderboards.snapshot(kind="ae")
    sdr = await leaderboards.snapshot(kind="sdr")
    return {
        "week": ae.get("week"),
        "ae": ae,
        "sdr": sdr,
    }


async def scorecards_weekly() -> dict[str, Any]:
    """Friday 17:00 ET: per-rep scorecards for every rep with graded calls this week.

    Phase 1: DMs gated behind Hutch review (RUNBOOK). Returns one payload per
    rep; the caller iterates + posts (or queues into an approval gate).
    """
    from agents.sales_reps.call_grader import storage
    storage.ensure_schema()
    # Pull distinct reps with grades in the last 7 days.
    from sqlalchemy import text
    from shared.db.connection import get_engine
    since = datetime.now(timezone.utc) - timedelta(days=7)
    with get_engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT rep_email FROM sales_reps_call_grades "
                "WHERE rep_email IS NOT NULL AND graded_at >= :s"
            ),
            {"s": since},
        ).fetchall()
    reps = [r[0] for r in rows]
    results: list[dict[str, Any]] = []
    for email in reps:
        try:
            results.append(await scorecards.for_rep(email))
        except Exception as e:  # noqa: BLE001 — isolate per-rep
            log.exception("scorecards_weekly: for_rep failed for %s", email)
            results.append({"rep_email": email, "error": f"{type(e).__name__}: {e}"})
    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_scorecards_weekly",
        target=f"reps:{len(reps)}",
        after={"reps_processed": len(results)},
    )
    return {"reps_processed": len(results), "scorecards": results}


# --------------------------------------------------------------- dispatch table + CLI

_TICKS: dict[str, Callable[[], Awaitable[dict[str, Any]]]] = {
    "grader_poll": grader_poll,
    "brief_scan": brief_scan,
    "hygiene_daily": hygiene_daily,
    "sync_check": sync_check,
    "risk_sweep": risk_sweep,
    "leaderboard_weekly": leaderboard_weekly,
    "scorecards_weekly": scorecards_weekly,
}


def run(tick_name: str) -> dict[str, Any]:
    """Synchronous entry point for launchd/Cloud Scheduler invocation."""
    fn = _TICKS.get(tick_name)
    if fn is None:
        raise SystemExit(f"unknown tick: {tick_name}; known: {sorted(_TICKS)}")
    return asyncio.run(fn())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.sales_reps.scheduler.jobs")
    parser.add_argument("tick", choices=sorted(_TICKS))
    parser.add_argument("--json", action="store_true", help="emit result as JSON on stdout")
    args = parser.parse_args(argv)
    result = run(args.tick)
    if args.json:
        print(json.dumps(result, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
