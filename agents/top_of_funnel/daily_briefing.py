"""Daily Briefing — 07:55 Mon–Fri SDR lead-list DMs + Hutch summary.

Cron entry: `send_daily_briefing()` (registered in shared.runtime.schedule via
Phase 0 amendment PR).

Flow:
  1. Pull tof_lead_candidates WHERE status='ready' AND run fresh (<4h old)
  2. Group by assigned_sdr_id
  3. DM each SDR: top 20 in body + full 200 in thread
  4. Summary DM to Hutch: counts per SDR, total, top-5 overall, exploration slot
  5. Mark candidates status='briefed'

Stale-pipeline guard: if latest run completed >4h ago or still running at 07:55,
post warning to O's DM instead of spamming SDRs with 0-lead briefings.

Ships D7.
"""
from __future__ import annotations

from typing import Any


async def send_daily_briefing() -> dict[str, Any]:
    """Cron entrypoint. Implemented D7."""
    raise NotImplementedError("send_daily_briefing ships D7")


async def send_dry_run(channel: str) -> dict[str, Any]:
    """Dry-run preview routed to channel (typically O's DM due to dev guard). Implemented D7."""
    return {"text": f"`daily dry-run` not yet wired (ships D7). Would target channel: {channel}"}
