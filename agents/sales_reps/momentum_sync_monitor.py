"""Momentum↔SF sync monitor — the 100%-hidden sync-break detector.

The Momentum plugin auto-logs each outbound call as a Salesforce Task.
When the sync breaks (expired auth, API change, webhook drop) calls keep
happening in Momentum but never land in SF — invisible to managers and
invisible to the AE's pipeline view. Hutch has asked for detection within
30 minutes of first break.

Detection logic
---------------
1. Pull last `LOOKBACK_HOURS` of Momentum calls.
2. For each call, look for a matching SF Task:
     - Momentum writes Task.CallObject = momentum call id, OR
     - Task.CreatedDate within ±5 min of call.started_at for same rep + contact.
3. A call is a "sync break" when Momentum has `sf_synced=False` OR no SF
   match is found after the GRACE_MINUTES delay (avoids flagging the
   natural async lag between call-end and SF task write).

Alerting
--------
Breaks are suppressed by the hourly rate gate `sales_reps_sync_alert_hourly`
(limit 1/hr/integration) to prevent alert storms during a full outage. Every
run audit-logs through governance, even when alerts are suppressed — so the
noise floor is silent but the forensic trail is complete.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from shared import governance
from shared.mcp import salesforce_mcp

from agents.sales_reps import rate_gates
from agents.sales_reps.integrations import momentum

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"

LOOKBACK_HOURS = 4
GRACE_MINUTES = 15
TIME_MATCH_WINDOW_MIN = 5


@dataclass
class SyncBreak:
    momentum_call_id: str
    started_at: str | None
    rep_email: str | None
    contact_email: str | None
    duration_seconds: int
    reason: str  # "sf_synced_false" | "no_sf_task_found"


# --------------------------------------------------------------- time helpers

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_grace(started_at: str | None, grace_minutes: int) -> bool:
    dt = _parse_iso(started_at)
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    # Only flag calls that ended LONG enough ago for SF sync to have completed.
    return (now - dt).total_seconds() >= grace_minutes * 60


# --------------------------------------------------------------- SF probe

def _find_sf_task(call: dict[str, Any]) -> dict[str, Any] | None:
    """Return an SF Task matching this Momentum call, or None."""
    call_id = call["id"]
    rep_email = call.get("rep_email")
    contact_email = call.get("contact_email")
    started_dt = _parse_iso(call.get("started_at"))

    # Preferred: Momentum writes CallObject = momentum call id on the Task.
    q1 = (
        "SELECT Id, CallObject, CreatedDate, WhoId, OwnerId "
        f"FROM Task WHERE CallObject = '{call_id}'"
    )
    try:
        rows = salesforce_mcp.soql_query(q1, limit=1).get("records", []) or []
    except Exception as e:  # noqa: BLE001 — don't blow up the whole sweep on a bad query
        log.warning("sync_check CallObject probe failed for %s: %s", call_id, e)
        rows = []
    if rows:
        return rows[0]

    # Fallback: time+rep+contact window match. Required when Momentum's CallObject
    # field is missing or renamed in this tenant.
    if not (rep_email and started_dt):
        return None
    start_low = (started_dt - timedelta(minutes=TIME_MATCH_WINDOW_MIN)).isoformat()
    start_high = (started_dt + timedelta(minutes=TIME_MATCH_WINDOW_MIN)).isoformat()
    where = [
        f"Owner.Email = '{rep_email}'",
        f"CreatedDate >= {start_low}",
        f"CreatedDate <= {start_high}",
        "Type = 'Call'",
    ]
    if contact_email:
        where.append(f"Who.Email = '{contact_email}'")
    q2 = "SELECT Id, CallObject, CreatedDate FROM Task WHERE " + " AND ".join(where)
    try:
        rows = salesforce_mcp.soql_query(q2, limit=1).get("records", []) or []
    except Exception as e:  # noqa: BLE001
        log.warning("sync_check time-window probe failed for %s: %s", call_id, e)
        return None
    return rows[0] if rows else None


def _detect_breaks(calls: list[dict[str, Any]]) -> list[SyncBreak]:
    breaks: list[SyncBreak] = []
    for call in calls:
        # Momentum itself tells us it couldn't sync — trust that first.
        if call.get("sf_synced") is False:
            breaks.append(SyncBreak(
                momentum_call_id=call["id"],
                started_at=call.get("started_at"),
                rep_email=call.get("rep_email"),
                contact_email=call.get("contact_email"),
                duration_seconds=call.get("duration_seconds", 0),
                reason="sf_synced_false",
            ))
            continue

        # Only check older calls — newer ones might still be in-flight.
        if not _within_grace(call.get("started_at"), GRACE_MINUTES):
            continue

        if _find_sf_task(call) is None:
            breaks.append(SyncBreak(
                momentum_call_id=call["id"],
                started_at=call.get("started_at"),
                rep_email=call.get("rep_email"),
                contact_email=call.get("contact_email"),
                duration_seconds=call.get("duration_seconds", 0),
                reason="no_sf_task_found",
            ))
    return breaks


# --------------------------------------------------------------- rendering

def _render_slack(
    calls_checked: int,
    breaks: list[SyncBreak],
    *,
    alert_suppressed: bool,
) -> str:
    if not breaks:
        return (
            f"*Momentum↔SF sync* ✓ — {calls_checked} calls checked in last "
            f"{LOOKBACK_HOURS}h, all synced."
        )
    header = (
        f"*Momentum↔SF SYNC BREAK* — {len(breaks)} call(s) missing from "
        f"SF in last {LOOKBACK_HOURS}h (of {calls_checked} checked)"
    )
    if alert_suppressed:
        header += "  _[alert suppressed — rate-gated]_"
    by_rep: dict[str, int] = {}
    for b in breaks:
        by_rep[b.rep_email or "(unknown)"] = by_rep.get(b.rep_email or "(unknown)", 0) + 1
    rep_line = "  · " + ", ".join(
        f"{rep}={n}" for rep, n in sorted(by_rep.items(), key=lambda x: -x[1])
    )
    lines = [header, rep_line, "\n*Sample*"]
    for b in breaks[:10]:
        started = (b.started_at or "")[:16].replace("T", " ")
        lines.append(
            f"   - `{b.momentum_call_id}` · {started} · "
            f"{b.rep_email or '?'} → {b.contact_email or '?'} · "
            f"{b.duration_seconds}s · _{b.reason}_"
        )
    if len(breaks) > 10:
        lines.append(f"   …and {len(breaks) - 10} more")
    return "\n".join(lines)


# --------------------------------------------------------------- public API

async def run_once() -> dict[str, Any]:
    """One sync check pass. Degrades on integration failures; never raises."""
    started = datetime.now(timezone.utc)

    try:
        calls = momentum.list_recent_calls(hours=LOOKBACK_HOURS, limit=500)
    except Exception as e:  # noqa: BLE001 — Momentum down is itself a signal, but not our break
        log.exception("sync_check: momentum fetch failed")
        governance.write_audit(
            agent_name=_AGENT_NAME,
            action="sales_reps_sync_check",
            target="momentum",
            after={"status": "momentum_unreachable", "error": str(e)[:200]},
        )
        return {
            "text": f"*Momentum↔SF sync* ⚠️ — Momentum API unreachable: {type(e).__name__}",
            "error": str(e),
            "calls_checked": 0,
            "breaks": [],
        }

    breaks = _detect_breaks(calls)

    # Alert suppression: only consume the hourly slot when we actually have
    # breaks to alert about. Check-but-clean runs don't tick the bucket.
    alert_suppressed = False
    if breaks:
        try:
            rate_gates.check("sales_reps_sync_alert_hourly", 3600)
        except rate_gates.RateGateExceeded:
            alert_suppressed = True
            log.info("sync_check: alert suppressed by hourly rate gate (%s breaks)", len(breaks))

    text_out = _render_slack(len(calls), breaks, alert_suppressed=alert_suppressed)

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_sync_check",
        target="momentum_sf",
        after={
            "calls_checked": len(calls),
            "breaks": len(breaks),
            "alert_suppressed": alert_suppressed,
            "by_reason": {
                r: sum(1 for b in breaks if b.reason == r)
                for r in {b.reason for b in breaks}
            },
            "duration_ms": int(
                (datetime.now(timezone.utc) - started).total_seconds() * 1000
            ),
        },
    )

    return {
        "text": text_out,
        "calls_checked": len(calls),
        "breaks": [asdict(b) for b in breaks],
        "alert_suppressed": alert_suppressed,
    }
