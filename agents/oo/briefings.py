"""Daily 8:30 briefing + Friday 4 PM weekly review.

Tier 7 of v0.7-hygiene plan adds three things to the original draft:
  - _compose_daily renders an "Urgent overnight" section (priority='urgent'
    OR category in {urgent_fire, automation_broken} created in last 16h)
  - _compose_weekly shows SUM(tokens_used) alongside cost
  - _send checks Google Calendar for an OOO event covering "now"; if one
    exists, the briefing is logged-and-skipped (not rescheduled).

OOO check is graceful: missing google-api-python-client OR missing
GOOGLE_SERVICE_ACCOUNT_JSON / GOOGLE_SERVICE_ACCOUNT_JSON_INLINE → log
"OOO check unavailable" and proceed with the send. Failure modes that
should silence the briefing must come from a real calendar event.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

_OOO_PATTERN = re.compile(r"\b(OOO|out of office|out-of-office|PTO|vacation|holiday|sick)\b", re.I)


def _compose_daily() -> str:
    engine = get_engine()
    cutoff_16h = datetime.now(timezone.utc) - timedelta(hours=16)
    with engine.begin() as conn:
        tasks = conn.execute(
            text(
                """SELECT title, priority, category, agent_name FROM tasks
                     WHERE status = 'pending'
                     ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                            WHEN 'medium' THEN 2 ELSE 3 END LIMIT 10"""
            )
        ).mappings().all()
        urgent_overnight = conn.execute(
            text(
                """SELECT title, category, agent_name, created_at FROM tasks
                     WHERE status = 'pending'
                       AND created_at >= :c
                       AND (
                           priority = 'urgent'
                           OR category IN ('urgent_fire', 'automation_broken', 'integration_broken')
                       )
                     ORDER BY created_at DESC LIMIT 10"""
            ),
            {"c": cutoff_16h},
        ).mappings().all()
        health = conn.execute(
            text(
                """SELECT integration, status, error_message FROM integration_health
                     WHERE (integration, checked_at) IN (
                        SELECT integration, MAX(checked_at) FROM integration_health GROUP BY integration
                     )"""
            )
        ).mappings().all()
        pending_gates = conn.execute(
            text("SELECT COUNT(*) FROM approval_gates WHERE status = 'pending'")
        ).scalar() or 0

    lines = [f"*Good morning, O.* Daily briefing — {datetime.now().strftime('%Y-%m-%d')}"]
    if urgent_overnight:
        lines.append("\n*Urgent overnight (last 16h):*")
        lines.extend(
            f"• [{u['category']}] {u['title']} — {u['agent_name']}"
            for u in urgent_overnight[:5]
        )
    if tasks:
        lines.append("\n*Priorities:*")
        lines.extend(f"• ({t['priority']}) {t['title']} — {t['agent_name']}" for t in tasks[:5])
    else:
        lines.append("\n*Priorities:* board is clear.")
    problems = [h for h in health if h["status"] != "healthy"]
    if problems:
        lines.append("\n*Integration issues:*")
        lines.extend(f"• {h['integration']}: {h['status']} — {h['error_message'] or ''}" for h in problems)
    else:
        lines.append("\n*Integrations:* all healthy.")
    if pending_gates:
        lines.append(f"\n*Approvals waiting:* {pending_gates}")
    lines.append("\nReady when you are.")
    return "\n".join(lines)


def _compose_weekly() -> str:
    engine = get_engine()
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with engine.begin() as conn:
        created = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE created_at >= :c"), {"c": cutoff}
        ).scalar() or 0
        completed = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE completed_at >= :c"), {"c": cutoff}
        ).scalar() or 0
        incidents = conn.execute(
            text(
                """SELECT integration, COUNT(*) n FROM integration_health
                     WHERE checked_at >= :c AND status != 'healthy' GROUP BY integration"""
            ),
            {"c": cutoff},
        ).mappings().all()
        gates = conn.execute(
            text("SELECT status, COUNT(*) n FROM approval_gates WHERE requested_at >= :c GROUP BY status"),
            {"c": cutoff},
        ).mappings().all()
        cost = conn.execute(
            text("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE started_at >= :c"),
            {"c": cutoff},
        ).scalar() or 0.0
        tokens = conn.execute(
            text("SELECT COALESCE(SUM(tokens_used), 0) FROM agent_runs WHERE started_at >= :c"),
            {"c": cutoff},
        ).scalar() or 0

    lines = [f"*Weekly Review — O* ({cutoff.date()} → {datetime.now().date()})"]
    lines.append(f"\nTasks: {created} created, {completed} completed")
    if incidents:
        lines.append("Integration incidents:")
        lines.extend(f"  • {i['integration']}: {i['n']}" for i in incidents)
    lines.append("Approvals: " + ", ".join(f"{g['status']}={g['n']}" for g in gates) or "Approvals: none")
    lines.append(f"Claude spend: ${cost:.2f}  •  Tokens: {int(tokens):,}")
    return "\n".join(lines)


def _is_user_ooo_now() -> bool:
    """Return True iff the user has an active OOO/PTO/vacation event right now.

    Graceful degrade: if google-api-python-client isn't installed OR the
    service-account creds aren't configured, returns False (proceed with
    send) rather than silencing the briefing on a config error. The OOO
    skip should only fire on a REAL calendar event.
    """
    if not (
        get_config("GOOGLE_SERVICE_ACCOUNT_JSON")
        or get_config("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE")
    ):
        return False
    try:
        from agents.sales_reps.integrations import gcal
    except ImportError:
        log.info("OOO check unavailable: google-api-python-client not installed")
        return False
    try:
        now = datetime.now(timezone.utc)
        cal = get_config("GCAL_OOO_CALENDAR_ID") or "primary"
        events = gcal.list_events(
            time_min=now - timedelta(minutes=5),
            time_max=now + timedelta(minutes=5),
            calendar_id=cal,
            max_results=20,
        )
    except Exception as e:
        log.warning("OOO check failed: %s — proceeding with briefing", e)
        return False
    for ev in events:
        title = ev.get("title") or ""
        desc = ev.get("description") or ""
        if _OOO_PATTERN.search(title) or _OOO_PATTERN.search(desc):
            log.info("OOO event matched — skipping briefing: %s", title)
            return True
    return False


async def send_daily_briefing(sender: Any | None = None) -> dict[str, Any]:
    msg = _compose_daily()
    return await _send(msg, sender)


async def send_weekly_review(sender: Any | None = None) -> dict[str, Any]:
    msg = _compose_weekly()
    return await _send(msg, sender)


async def _send(msg: str, sender: Any | None) -> dict[str, Any]:
    if _is_user_ooo_now():
        return {"ok": True, "skipped": "ooo", "preview": msg[:500]}
    target = get_config("SLACK_TEST_CHANNEL") or "U08K2UTG3G8"
    if sender is None:
        from shared.slack_dispatcher import SlackSender
        sender = SlackSender()
    try:
        result = sender.send(target, msg)
    except Exception as e:
        return {"ok": False, "error": str(e), "preview": msg[:500]}
    return {"ok": result.get("ok", False), "target": target, "preview": msg[:500]}


if __name__ == "__main__":
    print(asyncio.run(send_daily_briefing()))
