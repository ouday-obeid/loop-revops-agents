"""Daily 8:30 briefing + Friday 4 PM weekly review."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine


def _compose_daily() -> str:
    engine = get_engine()
    with engine.begin() as conn:
        tasks = conn.execute(
            text(
                """SELECT title, priority, category, agent_name FROM tasks
                     WHERE status = 'pending'
                     ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                            WHEN 'medium' THEN 2 ELSE 3 END LIMIT 10"""
            )
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

    lines = [f"*Weekly Review — O* ({cutoff.date()} → {datetime.now().date()})"]
    lines.append(f"\nTasks: {created} created, {completed} completed")
    if incidents:
        lines.append("Integration incidents:")
        lines.extend(f"  • {i['integration']}: {i['n']}" for i in incidents)
    lines.append("Approvals: " + ", ".join(f"{g['status']}={g['n']}" for g in gates) or "Approvals: none")
    lines.append(f"Claude spend: ${cost:.2f}")
    return "\n".join(lines)


async def send_daily_briefing(sender: Any | None = None) -> dict[str, Any]:
    msg = _compose_daily()
    return await _send(msg, sender)


async def send_weekly_review(sender: Any | None = None) -> dict[str, Any]:
    msg = _compose_weekly()
    return await _send(msg, sender)


async def _send(msg: str, sender: Any | None) -> dict[str, Any]:
    from shared.secrets import get_config
    target = get_config("SLACK_TEST_CHANNEL") or "U07P4GX9YLQ"
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
