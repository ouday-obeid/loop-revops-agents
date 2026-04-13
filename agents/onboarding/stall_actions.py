"""Slack block action handlers for stall alert buttons.

Wired by `agents.onboarding.main.bootstrap()` into the shared dispatcher.

Two buttons are attached to every stall alert posted by `milestone_monitor`:

- `stall_extend_3d`  — advances the dedup window for this onboarding by 3
  business days so the agent will not re-alert during that time. Writes a
  `stall_extended` row to `audit_log`.
- `stall_escalate`   — posts a louder DM to Jackie + O, and writes a
  `stall_escalated` row to `audit_log`. Does not touch the dedup window;
  the alert can still fire again after the normal 72h dedup expires.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text as sql_text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)

AGENT_NAME = "onboarding"
EXTEND_BUSINESS_DAYS = 3


def _advance_business_days(start: datetime, bdays: int) -> datetime:
    """Add `bdays` weekdays to `start`, preserving time-of-day and tz."""
    cursor: date = start.date()
    remaining = bdays
    while remaining > 0:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return datetime.combine(cursor, start.timetz())


def _extract_onboarding_id(body: dict[str, Any]) -> str | None:
    actions = body.get("actions") or []
    if not actions:
        return None
    return actions[0].get("value")


def _extract_user_id(body: dict[str, Any]) -> str:
    return (body.get("user") or {}).get("id") or "slack:unknown"


async def handle_extend_3d(body: dict[str, Any]) -> dict[str, Any]:
    """Push the stall-alert dedup window out by 3 business days."""
    onboarding_id = _extract_onboarding_id(body)
    if not onboarding_id:
        return {"text": "Missing onboarding id on the button payload."}
    user_id = _extract_user_id(body)

    from agents.onboarding.milestone_monitor import _ensure_dedup_table
    from shared.governance import write_audit

    _ensure_dedup_table()
    now = datetime.now(timezone.utc)
    new_last = _advance_business_days(now, EXTEND_BUSINESS_DAYS)

    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """UPDATE onboarding_stall_alerts
                      SET last_alerted_at = :t
                    WHERE onboarding_id = :id"""
            ),
            {"t": new_last, "id": onboarding_id},
        )

    write_audit(
        agent_name=AGENT_NAME,
        action="stall_extended",
        target=f"sf:Onboarding__c:{onboarding_id}",
        after={
            "extended_by_business_days": EXTEND_BUSINESS_DAYS,
            "silenced_until": new_last.isoformat(),
            "clicked_by": user_id,
        },
    )
    log.info(
        "stall extend_3d onboarding=%s by=%s silenced_until=%s",
        onboarding_id, user_id, new_last.isoformat(),
    )
    return {"text": (
        f"⏭ Snoozed `{onboarding_id}` stall alerts for "
        f"{EXTEND_BUSINESS_DAYS} business days (until {new_last.date().isoformat()})."
    )}


async def handle_escalate(body: dict[str, Any]) -> dict[str, Any]:
    """Escalate the stall alert to Jackie + O with a louder DM."""
    onboarding_id = _extract_onboarding_id(body)
    if not onboarding_id:
        return {"text": "Missing onboarding id on the button payload."}
    user_id = _extract_user_id(body)

    from shared.governance import write_audit
    from shared.secrets import get_config
    from shared.slack_dispatcher import SlackSender

    sender = SlackSender()
    jackie_channel = get_config("ONBOARDING_JACKIE_CHANNEL", "#agent-onboarding-log")
    o_dm = get_config("ONBOARDING_O_DM", "")

    text_ = (
        f"🔴 *Escalated stall* — `{onboarding_id}`\n"
        f"Escalated by `{user_id}`. This onboarding has been parked past "
        "the dedup window and needs hands-on attention."
    )
    sender.send(jackie_channel, text_)
    if o_dm:
        sender.send(o_dm, text_)

    write_audit(
        agent_name=AGENT_NAME,
        action="stall_escalated",
        target=f"sf:Onboarding__c:{onboarding_id}",
        after={"clicked_by": user_id},
    )
    log.info("stall escalated onboarding=%s by=%s", onboarding_id, user_id)
    return {"text": f"🔴 Escalated `{onboarding_id}` to Jackie + O."}
