"""Renewal stall monitor — flags Renewal opportunities stuck in the same stage.

Every morning this scans open Renewal opps, pulls `LastStageChangeDate` (Salesforce
standard field) and computes stall days. Anything ≥14d triggers a daily-idempotent
task for Blaine + a Slack alert. Stalls ≥30d escalate to Jackie.

Idempotency: task source includes the ISO date so one task per opp per day;
re-running the sweep within the same day is a no-op.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.slack_dispatcher import SlackSender

log = logging.getLogger(__name__)

ALERT_CHANNEL = "#agent-cs-log"
STALL_WARN_DAYS = 14
STALL_ESCALATE_DAYS = 30


def _parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _query_open_renewals(sf_mcp: Any) -> list[dict[str, Any]]:
    q = (
        "SELECT Id, Name, AccountId, Account.Name, OwnerId, StageName, "
        "LastStageChangeDate, LastModifiedDate, Zen_Contract_End_Date__c "
        "FROM Opportunity "
        "WHERE Type = 'Renewal' AND IsClosed = false"
    )
    r = sf_mcp.soql_query(q, limit=500)
    return r.get("records") or []


def _stall_days(opp: dict[str, Any], now: datetime) -> int | None:
    last = _parse_dt(opp.get("LastStageChangeDate")) or _parse_dt(opp.get("LastModifiedDate"))
    if not last:
        return None
    return (now - last).days


def _open_task(opp: dict[str, Any], stall_days: int, now: datetime, escalate: bool) -> None:
    oid = opp["Id"]
    source = f"cs:renewal_stall:{oid}:{now.date().isoformat()}"
    account_name = (opp.get("Account") or {}).get("Name") or opp.get("AccountId")
    priority = "urgent" if escalate else "high"
    title = f"Renewal stalled {stall_days}d in {opp.get('StageName')} — {account_name}"
    description = (
        f"Opportunity `{oid}` ({account_name}) has been in stage "
        f"`{opp.get('StageName')}` for {stall_days} days. Review and advance "
        "the stage, or reason about next steps with the CSM."
    )
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"), {"s": source}
        ).fetchone()
        if exists:
            return
        assignee = "jackie" if escalate else "blaine"
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('cs', :t, :d, 'pending', :p, 'renewal_stall', :s, :a, :m)"""
            ),
            {
                "t": title,
                "d": description,
                "p": priority,
                "s": source,
                "a": assignee,
                "m": json.dumps({"opportunity_id": oid, "stall_days": stall_days, "stage": opp.get("StageName")}),
            },
        )


def _alert(opp: dict[str, Any], stall_days: int, escalate: bool, sender: SlackSender) -> None:
    account_name = (opp.get("Account") or {}).get("Name") or opp.get("AccountId")
    mention = " @jackie" if escalate else ""
    body = (
        f":hourglass_flowing_sand: *Renewal stalled {stall_days}d* — {account_name}{mention}\n"
        f"Stage: `{opp.get('StageName')}` | Opp: `{opp['Id']}`\n"
        f"Contract end: `{opp.get('Zen_Contract_End_Date__c')}`"
    )
    sender.send(ALERT_CHANNEL, body)


async def run_sweep(
    *,
    sf_mcp: Any | None = None,
    slack_sender: SlackSender | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf
    sender = slack_sender or SlackSender()

    counters = {"open_renewals": 0, "warn": 0, "escalate": 0, "skipped_today": 0}
    for opp in _query_open_renewals(sf_mcp):
        counters["open_renewals"] += 1
        stall = _stall_days(opp, now)
        if stall is None or stall < STALL_WARN_DAYS:
            continue
        escalate = stall >= STALL_ESCALATE_DAYS
        # Dedup per-day: check the task source before alerting.
        source = f"cs:renewal_stall:{opp['Id']}:{now.date().isoformat()}"
        engine = get_engine()
        with engine.begin() as conn:
            dupe = conn.execute(
                text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"), {"s": source}
            ).fetchone()
        if dupe:
            counters["skipped_today"] += 1
            continue
        _open_task(opp, stall, now, escalate)
        _alert(opp, stall, escalate, sender)
        counters["escalate" if escalate else "warn"] += 1

    log.info("cs-renewal-stall complete: %s", counters)
    return counters
