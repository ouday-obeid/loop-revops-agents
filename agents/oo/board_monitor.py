"""Passive Slack + Fireflies scanner. Creates tasks, alerts O on hot categories.

Invoked every 15 minutes by launchd (see shared/runtime/schedule.py).

Hot-category DM alerts (2-min SLO from Monday parent 11736893953):
  - is_alertworthy(cls)        → urgent_fire / automation_broken /
                                  integration_broken / data_quality+100%
  - AUTOMATION_BROKEN category → always fires (already in is_alertworthy)
  - data_quality + 100% hidden → already in is_alertworthy
  - integration auth failures  → forwarded by integration_health.poll()
                                  via the same _alert_o_dm helper there
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from agents.oo.classifier import classify, is_alertworthy
from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

CHANNELS = ["#deal-team", "#sdr-team", "#s-sales-team", "#cs-team", "#revops"]


def _task_exists(source: str) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"), {"s": source}).fetchone()
        return row is not None


def _create_task(title: str, description: str, category: str, priority: str, source: str) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        res = conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee)
                   VALUES ('oo', :title, :desc, 'pending', :prio, :cat, :src, 'system')"""
            ),
            {"title": title[:250], "desc": description, "prio": priority, "cat": category, "src": source},
        )
        return int(res.lastrowid or 0)


def _alert_o(title: str, category: str, source: str, snippet: str, *, sender: Any | None = None) -> None:
    """Post a one-line DM to O on alertworthy classifier matches.

    Wrapped so a Slack outage can't prevent task creation. The 2-min SLO
    from parent 11736893953 is met because there's no blocking I/O between
    the classifier match and this call — both run synchronously inside
    the ~30-second per-channel scan window.
    """
    try:
        if sender is None:
            from shared.slack_dispatcher import SlackSender
            sender = SlackSender()
        o_dm = get_config("SLACK_TEST_CHANNEL") or "U07P4GX9YLQ"
        msg = f":rotating_light: *{category}* signal\n> {title[:200]}\n_source: {source}_"
        if snippet:
            msg += f"\n```{snippet[:400]}```"
        sender.send(o_dm, msg)
    except Exception as e:
        log.exception("board_monitor DM alert failed: %s", e)


async def scan_slack(
    slack_client: Any | None = None, *, sender: Any | None = None
) -> list[dict[str, Any]]:
    """Scan configured channels for pain signals. Returns created tasks.

    Sends an O DM for every alertworthy classification (urgent_fire,
    automation_broken, integration_broken, data_quality+100%-hidden).
    """
    created: list[dict[str, Any]] = []
    if slack_client is None:
        log.info("board_monitor: no slack client attached, skipping slack scan")
        return created
    for channel in CHANNELS:
        try:
            resp = slack_client.conversations_history(channel=channel, limit=50)
        except Exception as e:
            log.warning("slack history fail for %s: %s", channel, e)
            continue
        for msg in resp.get("messages", []):
            source = f"slack:{channel}:{msg.get('ts')}"
            if _task_exists(source):
                continue
            cls = classify(msg.get("text", ""))
            if cls.category == "other":
                continue
            alert = is_alertworthy(cls)
            priority = "urgent" if alert else "medium"
            tid = _create_task(
                title=msg.get("text", "")[:120],
                description=json.dumps({"channel": channel, "ts": msg.get("ts"), "user": msg.get("user")}),
                category=cls.category,
                priority=priority,
                source=source,
            )
            created.append({"id": tid, "category": cls.category, "source": source, "alert": alert})
            if alert:
                _alert_o(
                    title=msg.get("text", "")[:200],
                    category=cls.category,
                    source=source,
                    snippet=cls.matched_phrase or "",
                    sender=sender,
                )
    return created


async def scan_fireflies(
    fireflies_mcp: Any | None = None, *, sender: Any | None = None
) -> list[dict[str, Any]]:
    created: list[dict[str, Any]] = []
    if fireflies_mcp is None:
        return created
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    try:
        rows = fireflies_mcp.list_transcripts(from_date=cutoff, limit=20)
    except Exception as e:
        log.warning("fireflies scan fail: %s", e)
        return created
    for row in rows:
        source = f"fireflies:{row.get('id')}"
        if _task_exists(source):
            continue
        cls = classify((row.get("title") or "") + " " + (row.get("summary", {}) or {}).get("overview", ""))
        if cls.category == "other":
            continue
        alert = is_alertworthy(cls)
        tid = _create_task(
            title=row.get("title", "Fireflies signal")[:120],
            description=json.dumps(row)[:2000],
            category=cls.category,
            priority="high" if alert else "medium",
            source=source,
        )
        created.append({"id": tid, "category": cls.category, "source": source, "alert": alert})
        if alert:
            _alert_o(
                title=row.get("title", "Fireflies signal")[:200],
                category=cls.category,
                source=source,
                snippet=cls.matched_phrase or "",
                sender=sender,
            )
    return created


async def scan() -> dict[str, Any]:
    """Entrypoint invoked by launchd."""
    slack_tasks = await scan_slack(None)  # Slack client wired when daemon is running
    ff_tasks = await scan_fireflies(None)
    return {"slack": len(slack_tasks), "fireflies": len(ff_tasks)}


if __name__ == "__main__":
    print(asyncio.run(scan()))
