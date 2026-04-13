"""Deal-risk signals — event-driven sweep every ~2h.

Four signals (all read-only):
  - pushed_close: CloseDate moved forward > PUSHED_CLOSE_THRESHOLD_DAYS in last 30d
  - amount_drop: Amount decreased > AMOUNT_DROP_PCT (0.20 default) in last 30d
  - champion_gone: a primary contact role on the opp was deactivated in last 30d
  - competitor_mention: recent Fireflies transcript mentions a competitor name

Each finding includes opportunity_id, signal type, severity, and a short
evidence line the rep/manager can act on. Output is Slack-renderable and
audit-logged; no SF writes.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from shared import governance
from shared.mcp import fireflies_mcp, salesforce_mcp

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"

PUSHED_CLOSE_THRESHOLD_DAYS = 15
AMOUNT_DROP_PCT = 0.20
HISTORY_WINDOW_DAYS = 30

# Known competitors — short list; pulls from RUNBOOK on deploy. Case-insensitive.
_COMPETITORS: tuple[str, ...] = (
    "Restaurant365", "R365", "Margin Edge", "MarginEdge",
    "Otter", "Deliverect", "Ordermark",
    "Olo", "Chowly", "Toast Delivery",
)


@dataclass
class RiskSignal:
    opportunity_id: str
    opportunity_name: str
    owner_email: str | None
    stage: str
    amount: float | None
    signal: str          # pushed_close | amount_drop | champion_gone | competitor_mention
    severity: str        # info | warn | high
    evidence: str


# --------------------------------------------------------------- helpers

def _owner_email(row: dict[str, Any]) -> str | None:
    return ((row.get("Owner") or {}).get("Email") or "").lower() or None


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


# --------------------------------------------------------------- pushed_close

def _detect_pushed_close() -> list[RiskSignal]:
    """Detect opps whose CloseDate moved forward via OpportunityFieldHistory."""
    since = _since(HISTORY_WINDOW_DAYS)
    # Pull opp field history where CloseDate changed recently.
    hist_q = (
        "SELECT OpportunityId, OldValue, NewValue, CreatedDate "
        "FROM OpportunityFieldHistory "
        f"WHERE Field = 'CloseDate' AND CreatedDate >= {since}T00:00:00Z"
    )
    history = salesforce_mcp.soql_query(hist_q, limit=500).get("records", []) or []
    if not history:
        return []

    affected_ids = sorted({h["OpportunityId"] for h in history})
    ids_clause = "(" + ",".join(f"'{i}'" for i in affected_ids) + ")"
    opp_q = (
        "SELECT Id, Name, StageName, Amount, CloseDate, IsClosed, "
        "Owner.Email "
        f"FROM Opportunity WHERE Id IN {ids_clause} AND IsClosed = false"
    )
    opps = {
        o["Id"]: o
        for o in salesforce_mcp.soql_query(opp_q, limit=500).get("records", []) or []
    }

    # For each opp, find the max forward-movement in days.
    max_push: dict[str, int] = {}
    for h in history:
        opp_id = h["OpportunityId"]
        try:
            old = datetime.fromisoformat(h["OldValue"]) if h["OldValue"] else None
            new = datetime.fromisoformat(h["NewValue"]) if h["NewValue"] else None
        except (TypeError, ValueError):
            continue
        if old is None or new is None:
            continue
        delta = (new - old).days
        if delta > 0:
            max_push[opp_id] = max(max_push.get(opp_id, 0), delta)

    out: list[RiskSignal] = []
    for opp_id, days in max_push.items():
        if days < PUSHED_CLOSE_THRESHOLD_DAYS:
            continue
        opp = opps.get(opp_id)
        if not opp:
            continue
        out.append(RiskSignal(
            opportunity_id=opp_id,
            opportunity_name=opp.get("Name", ""),
            owner_email=_owner_email(opp),
            stage=opp.get("StageName", ""),
            amount=opp.get("Amount"),
            signal="pushed_close",
            severity="high" if days >= 30 else "warn",
            evidence=f"CloseDate pushed +{days}d in last {HISTORY_WINDOW_DAYS}d",
        ))
    return out


# --------------------------------------------------------------- amount_drop

def _detect_amount_drops() -> list[RiskSignal]:
    since = _since(HISTORY_WINDOW_DAYS)
    hist_q = (
        "SELECT OpportunityId, OldValue, NewValue, CreatedDate "
        "FROM OpportunityFieldHistory "
        f"WHERE Field = 'Amount' AND CreatedDate >= {since}T00:00:00Z"
    )
    history = salesforce_mcp.soql_query(hist_q, limit=500).get("records", []) or []
    if not history:
        return []

    affected_ids = sorted({h["OpportunityId"] for h in history})
    ids_clause = "(" + ",".join(f"'{i}'" for i in affected_ids) + ")"
    opp_q = (
        "SELECT Id, Name, StageName, Amount, CloseDate, IsClosed, Owner.Email "
        f"FROM Opportunity WHERE Id IN {ids_clause} AND IsClosed = false"
    )
    opps = {
        o["Id"]: o
        for o in salesforce_mcp.soql_query(opp_q, limit=500).get("records", []) or []
    }

    out: list[RiskSignal] = []
    for h in history:
        try:
            old_amt = float(h["OldValue"]) if h["OldValue"] not in (None, "") else None
            new_amt = float(h["NewValue"]) if h["NewValue"] not in (None, "") else None
        except (TypeError, ValueError):
            continue
        if not old_amt or not new_amt:
            continue
        drop_pct = (old_amt - new_amt) / old_amt
        if drop_pct < AMOUNT_DROP_PCT:
            continue
        opp = opps.get(h["OpportunityId"])
        if not opp:
            continue
        out.append(RiskSignal(
            opportunity_id=opp["Id"],
            opportunity_name=opp.get("Name", ""),
            owner_email=_owner_email(opp),
            stage=opp.get("StageName", ""),
            amount=opp.get("Amount"),
            signal="amount_drop",
            severity="high" if drop_pct >= 0.5 else "warn",
            evidence=f"Amount dropped {drop_pct*100:.0f}% ({old_amt:.0f} → {new_amt:.0f})",
        ))
    return out


# --------------------------------------------------------------- champion_gone

def _detect_champion_gone() -> list[RiskSignal]:
    """Primary OpportunityContactRole whose Contact went inactive recently."""
    since = _since(HISTORY_WINDOW_DAYS)
    q = (
        "SELECT Id, OpportunityId, Role, IsPrimary, Contact.Id, Contact.Name, "
        "Contact.Active__c, Contact.LastModifiedDate, "
        "Opportunity.Name, Opportunity.StageName, Opportunity.Amount, "
        "Opportunity.IsClosed, Opportunity.Owner.Email "
        "FROM OpportunityContactRole WHERE IsPrimary = true "
        f"AND Contact.Active__c = false AND Contact.LastModifiedDate >= {since}T00:00:00Z"
    )
    try:
        rows = salesforce_mcp.soql_query(q, limit=200).get("records", []) or []
    except Exception as e:  # noqa: BLE001 — Active__c may not exist in all orgs
        log.warning("champion_gone query failed (skipping): %s", e)
        return []

    out: list[RiskSignal] = []
    for r in rows:
        opp = r.get("Opportunity") or {}
        if opp.get("IsClosed"):
            continue
        contact = r.get("Contact") or {}
        out.append(RiskSignal(
            opportunity_id=r["OpportunityId"],
            opportunity_name=opp.get("Name", ""),
            owner_email=((opp.get("Owner") or {}).get("Email") or "").lower() or None,
            stage=opp.get("StageName", ""),
            amount=opp.get("Amount"),
            signal="champion_gone",
            severity="high",
            evidence=f"Primary contact {contact.get('Name', '?')} deactivated",
        ))
    return out


# --------------------------------------------------------------- competitor_mention

def _competitor_pattern() -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(c) for c in _COMPETITORS) + r")\b", re.I)


def _detect_competitor_mentions(lookback_hours: int = 26) -> list[RiskSignal]:
    """Scan recent Fireflies transcripts for competitor mentions, link to opp by email match."""
    # Fireflies' list endpoint is date-based. Use the last day + one hour for overlap safety.
    from_date = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).date().isoformat()
    try:
        rows = fireflies_mcp.list_transcripts(from_date=from_date, limit=200)
    except Exception as e:  # noqa: BLE001 — fireflies may be unavailable
        log.warning("competitor scan skipped — fireflies unavailable: %s", e)
        return []

    pattern = _competitor_pattern()
    out: list[RiskSignal] = []
    for row in rows:
        # Full transcript fetch needed to see sentences — do this lazily and cheap.
        try:
            t = fireflies_mcp.get_transcript(row.get("id"))
        except Exception as e:  # noqa: BLE001
            log.debug("skipping transcript %s: %s", row.get("id"), e)
            continue
        sentences = t.get("sentences") or []
        text_blob = " ".join(s.get("text", "") for s in sentences[:400])
        match = pattern.search(text_blob)
        if not match:
            continue
        # Try to connect transcript to an opp via external attendees.
        participants = t.get("participants") or []
        if isinstance(participants, str):
            participants = [p.strip() for p in participants.split(",")]
        external_emails = [
            p for p in participants
            if p and "@" in p and not p.endswith("@tryloop.ai")
        ]
        opp = _opp_for_attendees(external_emails)
        if not opp:
            continue
        out.append(RiskSignal(
            opportunity_id=opp["Id"],
            opportunity_name=opp.get("Name", ""),
            owner_email=_owner_email(opp),
            stage=opp.get("StageName", ""),
            amount=opp.get("Amount"),
            signal="competitor_mention",
            severity="warn",
            evidence=f"Competitor mentioned: {match.group(0)} (meeting {row.get('id')})",
        ))
    return out


def _opp_for_attendees(emails: list[str]) -> dict[str, Any] | None:
    """Find the most recent open Opportunity whose Contacts include any of these emails."""
    if not emails:
        return None
    emails_clause = "(" + ", ".join(f"'{e}'" for e in emails[:10]) + ")"
    q = (
        "SELECT OpportunityId, Opportunity.Name, Opportunity.StageName, "
        "Opportunity.Amount, Opportunity.IsClosed, Opportunity.Owner.Email "
        "FROM OpportunityContactRole "
        f"WHERE Contact.Email IN {emails_clause} "
        "AND Opportunity.IsClosed = false "
        "ORDER BY Opportunity.LastModifiedDate DESC"
    )
    try:
        rows = salesforce_mcp.soql_query(q, limit=5).get("records", []) or []
    except Exception:
        return None
    if not rows:
        return None
    first = rows[0]
    opp = first.get("Opportunity") or {}
    return {
        "Id": first["OpportunityId"],
        "Name": opp.get("Name", ""),
        "StageName": opp.get("StageName", ""),
        "Amount": opp.get("Amount"),
        "Owner": opp.get("Owner") or {},
    }


# --------------------------------------------------------------- rendering

def _render_slack(signals: list[RiskSignal]) -> str:
    if not signals:
        return "*Deal risk sweep*: no new signals ✓"

    by_severity = {"high": [], "warn": [], "info": []}
    for s in signals:
        by_severity.setdefault(s.severity, []).append(s)

    lines = [
        f"*Deal risk sweep* — {len(signals)} signals",
        "  · " + " · ".join(
            f"{sev}={len(by_severity[sev])}"
            for sev in ("high", "warn", "info") if by_severity[sev]
        ),
    ]
    for sev in ("high", "warn", "info"):
        items = by_severity.get(sev, [])
        if not items:
            continue
        lines.append(f"\n*{sev.upper()}* ({len(items)})")
        for sig in items[:20]:
            amt = f"${sig.amount:,.0f}" if sig.amount else "—"
            lines.append(
                f"   - `{sig.opportunity_id}` {sig.opportunity_name[:40]} · "
                f"{sig.stage} · {amt} · *{sig.signal}* · _{sig.evidence}_"
            )
        if len(items) > 20:
            lines.append(f"   …and {len(items) - 20} more")
    return "\n".join(lines)


# --------------------------------------------------------------- public API

async def run_sweep() -> dict[str, Any]:
    """Run all four detectors. Returns Slack payload + raw signals."""
    signals: list[RiskSignal] = []
    errors: list[str] = []

    for name, fn in (
        ("pushed_close", _detect_pushed_close),
        ("amount_drop", _detect_amount_drops),
        ("champion_gone", _detect_champion_gone),
        ("competitor_mention", _detect_competitor_mentions),
    ):
        try:
            signals.extend(fn())
        except Exception as e:  # noqa: BLE001 — one detector failing must not stop the sweep
            log.exception("deal-risk detector %s failed", name)
            errors.append(f"{name}:{type(e).__name__}")

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_deal_risk_sweep",
        target="pipeline",
        after={
            "total_signals": len(signals),
            "by_signal": {
                s: sum(1 for x in signals if x.signal == s)
                for s in {x.signal for x in signals}
            },
            "errors": errors,
        },
    )

    return {
        "text": _render_slack(signals),
        "total_signals": len(signals),
        "errors": errors,
        "signals": [
            {
                "opportunity_id": s.opportunity_id,
                "opportunity_name": s.opportunity_name,
                "owner_email": s.owner_email,
                "stage": s.stage,
                "amount": s.amount,
                "signal": s.signal,
                "severity": s.severity,
                "evidence": s.evidence,
            }
            for s in signals
        ],
    }
