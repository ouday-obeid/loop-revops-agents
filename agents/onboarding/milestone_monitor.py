"""Onboarding milestone monitor — stall detection across two stage fields.

Scheduled every 6 hours. Reviews every `Onboarding__c` whose
`Overall_Onboarding_Status__c` is `Not Started` or `In Progress` and flags any
where BOTH `JK_Onboarding_Stage__c` AND `Overall_Onboarding_Status__c` have
been parked for ≥5 business days.

"Parked" is inferred from the `DS_*` date-stamp family: each DS column is
auto-populated by SF flow as stages advance (e.g.
`DS_Overall_Onboarding_Status_In_Progress__c`). We take the most recent DS
stamp relevant to the current stage as the "last advanced" timestamp; if no
DS stamp exists yet (brand-new record), we fall back to `LastModifiedDate`.

Dedup: one alert per (onboarding_id, stage_fingerprint) every 72h. The
fingerprint combines both stage values so that advancing EITHER field clears
the dedup window.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text as sql_text

from agents.onboarding import queries
from shared.db.connection import get_engine
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

AGENT_NAME = "onboarding"
DEFAULT_STALL_THRESHOLD_BUSINESS_DAYS = 5
ALERT_DEDUP_WINDOW = timedelta(hours=72)


# ---------- Business-day math ----------

def business_days_between(start: date, end: date) -> int:
    """Inclusive-exclusive: days that are weekdays (Mon-Fri) in [start, end).

    Holidays are not tracked — Loop AI's onboardings span US + international
    locations, so a US-only holiday list would be wrong anyway. Jackie can
    tune the threshold via `@oo onboarding stalls 7` if long holidays are
    inflating the alert volume.
    """
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


# ---------- Last-advanced inference ----------

def _last_advanced_from_ds_family(rec: dict[str, Any]) -> datetime | None:
    """Return the most recent DS_* timestamp from the record, or None."""
    candidates: list[datetime] = []
    for key, value in rec.items():
        if not key.startswith("DS_") or value is None:
            continue
        dt = _parse_sf_datetime(value)
        if dt:
            candidates.append(dt)
    return max(candidates) if candidates else None


def _parse_sf_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    # SF returns ISO 8601. LastModifiedDate is full datetime; DS_* are dates.
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------- Dedup store ----------

def _ensure_dedup_table() -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """CREATE TABLE IF NOT EXISTS onboarding_stall_alerts (
                        onboarding_id TEXT NOT NULL,
                        stage_fingerprint TEXT NOT NULL,
                        last_alerted_at TIMESTAMP NOT NULL,
                        PRIMARY KEY (onboarding_id, stage_fingerprint)
                    )"""
            )
        )


def _stage_fingerprint(rec: dict[str, Any]) -> str:
    return (
        f"jk={rec.get('JK_Onboarding_Stage__c') or '—'};"
        f"overall={rec.get('Overall_Onboarding_Status__c') or '—'}"
    )


def _recently_alerted(onboarding_id: str, fingerprint: str) -> bool:
    _ensure_dedup_table()
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                """SELECT last_alerted_at FROM onboarding_stall_alerts
                    WHERE onboarding_id = :id AND stage_fingerprint = :fp"""
            ),
            {"id": onboarding_id, "fp": fingerprint},
        ).fetchone()
    if not row or not row[0]:
        return False
    last = row[0]
    if isinstance(last, str):
        last = datetime.fromisoformat(last.replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) < ALERT_DEDUP_WINDOW


def _record_alert(onboarding_id: str, fingerprint: str) -> None:
    _ensure_dedup_table()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """INSERT INTO onboarding_stall_alerts
                        (onboarding_id, stage_fingerprint, last_alerted_at)
                    VALUES (:id, :fp, :t)
                    ON CONFLICT (onboarding_id, stage_fingerprint)
                    DO UPDATE SET last_alerted_at = EXCLUDED.last_alerted_at"""
            ),
            {"id": onboarding_id, "fp": fingerprint, "t": datetime.now(timezone.utc)},
        )


# ---------- Stall evaluation ----------

@dataclass
class Stall:
    onboarding_id: str
    name: str
    owner_id: str | None
    csm_2_id: str | None
    account_id: str | None
    jk_stage: str | None
    overall: str | None
    days_stalled: int
    last_advanced_at: datetime | None
    fingerprint: str


def evaluate_stall(
    rec: dict[str, Any],
    *,
    threshold_bdays: int = DEFAULT_STALL_THRESHOLD_BUSINESS_DAYS,
    now: datetime | None = None,
) -> Stall | None:
    """Return a Stall if the record is parked beyond threshold, else None."""
    now = now or datetime.now(timezone.utc)
    last_advanced = (
        _last_advanced_from_ds_family(rec)
        or _parse_sf_datetime(rec.get("LastModifiedDate"))
    )
    if last_advanced is None:
        return None
    days = business_days_between(last_advanced.date(), now.date())
    if days < threshold_bdays:
        return None
    return Stall(
        onboarding_id=rec["Id"],
        name=rec.get("Name") or "(unnamed)",
        owner_id=rec.get("OwnerId"),
        csm_2_id=rec.get("CSM_2__c"),
        account_id=(rec.get("Opportunity__r") or {}).get("AccountId"),
        jk_stage=rec.get("JK_Onboarding_Stage__c"),
        overall=rec.get("Overall_Onboarding_Status__c"),
        days_stalled=days,
        last_advanced_at=last_advanced,
        fingerprint=_stage_fingerprint(rec),
    )


# ---------- Public entry points ----------

async def find_stalls(
    *,
    min_business_days: int = DEFAULT_STALL_THRESHOLD_BUSINESS_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Query-only — returns stall dicts for the dispatcher."""
    res = salesforce_mcp.soql_query(queries.ACTIVE_ONBOARDINGS)
    rows = res.get("records") or []
    stalls: list[Stall] = []
    for rec in rows:
        stall = evaluate_stall(rec, threshold_bdays=min_business_days, now=now)
        if stall:
            stalls.append(stall)
    return [
        {
            "id": s.onboarding_id,
            "name": s.name,
            "jk_stage": s.jk_stage or "—",
            "overall": s.overall or "—",
            "days": s.days_stalled,
            "owner": s.owner_id,
            "csm_2": s.csm_2_id,
            "last_advanced_at": s.last_advanced_at.isoformat() if s.last_advanced_at else None,
        }
        for s in stalls
    ]


async def scan(*, now: datetime | None = None) -> dict[str, Any]:
    """Scheduled scan — fires alerts for newly stalled onboardings.

    Returns a summary dict: {"checked": N, "alerted": N, "skipped_dedup": N}.
    """
    from shared.governance import write_audit
    from shared.slack_dispatcher import SlackSender

    res = salesforce_mcp.soql_query(queries.ACTIVE_ONBOARDINGS)
    rows = res.get("records") or []
    sender = SlackSender()

    summary = {"checked": len(rows), "alerted": 0, "skipped_dedup": 0}
    for rec in rows:
        stall = evaluate_stall(rec, now=now)
        if stall is None:
            continue
        if _recently_alerted(stall.onboarding_id, stall.fingerprint):
            summary["skipped_dedup"] += 1
            continue
        _post_stall_alert(sender, stall)
        _record_alert(stall.onboarding_id, stall.fingerprint)
        write_audit(
            agent_name=AGENT_NAME,
            action="stall_alert",
            target=f"sf:Onboarding__c:{stall.onboarding_id}",
            after={
                "jk_stage": stall.jk_stage,
                "overall": stall.overall,
                "days_stalled": stall.days_stalled,
            },
        )
        summary["alerted"] += 1
    log.info("milestone scan: %s", summary)
    return summary


def _post_stall_alert(sender: Any, stall: Stall) -> None:
    from shared.secrets import get_config

    csm_target = _resolve_csm_dm(stall.owner_id)
    jackie_channel = get_config("ONBOARDING_JACKIE_CHANNEL", "#agent-onboarding-log")

    header = (
        f"🟡 *Stalled onboarding* — {stall.name}\n"
        f"• JK stage: `{stall.jk_stage or '—'}`\n"
        f"• Overall: `{stall.overall or '—'}`\n"
        f"• Days stalled: *{stall.days_stalled}* business days\n"
        f"• CSM: `{stall.owner_id or 'UNASSIGNED'}`"
    )
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
        {
            "type": "actions",
            "block_id": f"stall_{stall.onboarding_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Extend by 3 days"},
                    "action_id": "stall_extend_3d",
                    "value": stall.onboarding_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Escalate to Jackie + O"},
                    "style": "danger",
                    "action_id": "stall_escalate",
                    "value": stall.onboarding_id,
                },
            ],
        },
    ]
    if csm_target:
        sender.send(csm_target, header, blocks=blocks)
    sender.send(jackie_channel, header, blocks=blocks)


def _resolve_csm_dm(owner_id: str | None) -> str | None:
    """Map SF OwnerId → Slack user id.

    In Phase 3, the mapping lives in a config lookup (env var JSON). If the
    env var isn't set, we fall back to posting only to the Jackie channel so
    the alert doesn't silently vanish.
    """
    if not owner_id:
        return None
    from shared.secrets import get_config
    import json as _json

    raw = get_config("ONBOARDING_CSM_SLACK_MAP", "")
    if not raw:
        return None
    try:
        mapping = _json.loads(raw)
    except ValueError:
        log.warning("ONBOARDING_CSM_SLACK_MAP not valid JSON")
        return None
    return mapping.get(owner_id)
