"""OO dispatcher — routes @oo commands to specialists or handles directly."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from shared.agent_base import AgentBase
from shared.db.connection import get_engine

# Specialist stubs — only names that haven't registered a live handler yet.
# Once an agent registers via shared.slack_dispatcher.register(), the shared
# dispatcher routes directly and OO never sees the message; we keep the name
# here only so unreleased agents return a polite "not yet deployed" note.
SPECIALISTS = {
    "top_of_funnel",
    "onboarding",
    "cs",
}


class OODispatcher(AgentBase):
    def __init__(self):
        super().__init__(name="oo", slack_channel="oo-dm", monthly_token_budget=5_000_000)

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        text_in = (payload.get("text") or "").strip()
        lower = text_in.lower()
        if not text_in or lower == "ping":
            return {"text": "pong — OO online."}
        if lower.startswith("what'") or "board" in lower or lower == "tasks":
            return await self._board_summary()
        if lower == "health":
            return await self._health_summary()
        # Specialist routing
        parts = text_in.split(maxsplit=1)
        if parts[0].lower() in SPECIALISTS:
            return {
                "text": f"Specialist `{parts[0]}` is not yet deployed (Phase 1). "
                        f"Queued as task for when it comes online."
            }
        return {"text": f"OO received: _{text_in}_ (no handler wired — Phase 1 will route this)."}

    async def _board_summary(self) -> dict[str, Any]:
        engine = get_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    """SELECT id, agent_name, title, priority, category
                         FROM tasks WHERE status = 'pending'
                         ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                                WHEN 'medium' THEN 2 ELSE 3 END, id LIMIT 10"""
                )
            ).mappings().all()
        if not rows:
            return {"text": "Board is clear. Nothing pending."}
        lines = [f"• *{r['title']}* ({r['agent_name']}, {r['priority']}, {r['category']})" for r in rows]
        return {"text": "Open tasks:\n" + "\n".join(lines)}

    async def _health_summary(self) -> dict[str, Any]:
        engine = get_engine()
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    """SELECT integration, status, error_message, checked_at
                         FROM integration_health
                         WHERE (integration, checked_at) IN (
                            SELECT integration, MAX(checked_at) FROM integration_health GROUP BY integration
                         )"""
                )
            ).mappings().all()
        if not rows:
            return {"text": "No integration health data yet — poller hasn't run."}
        lines = [
            f"{'✅' if r['status']=='healthy' else '⚠️' if r['status']=='degraded' else '🔴'} "
            f"{r['integration']}: {r['status']}"
            + (f" — {r['error_message']}" if r['error_message'] else "")
            for r in rows
        ]
        return {"text": "Integrations:\n" + "\n".join(lines)}


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    return await OODispatcher().run(trigger="slack", payload=payload)
