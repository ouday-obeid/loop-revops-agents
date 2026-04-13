"""Pre-demo scan — find GCal events that look like demos starting in ~2h.

Scheduled every 15 min by launchd. For each upcoming demo:
  1. Pull external attendees (non-@tryloop.ai).
  2. Resolve to SF Opportunity via OpportunityContactRole.Contact.Email.
  3. Hand off to `brief_generator.generate(opp_id)`.

Title filter is intentionally loose: we include anything containing
"demo", "intro", "call with", or the Loop AI calendar category prefix.
False positives land as no-op briefs; false negatives would silently
miss briefs, which is the worse failure mode.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from shared.mcp import salesforce_mcp

from agents.sales_reps.integrations import gcal

log = logging.getLogger(__name__)

_DEMO_TITLE_PATTERN = re.compile(
    r"\b(demo|intro|kickoff|discovery|qbr|loop\b.*\bdemo|call with)\b",
    re.IGNORECASE,
)

_INTERNAL_DOMAIN = "@tryloop.ai"


def is_demo(event: dict[str, Any]) -> bool:
    title = (event.get("title") or "").strip()
    if not title:
        return False
    # Must have at least one external attendee, or it's an internal sync.
    if not external_attendees(event):
        return False
    return bool(_DEMO_TITLE_PATTERN.search(title))


def external_attendees(event: dict[str, Any]) -> list[str]:
    return [
        a["email"]
        for a in event.get("attendees") or []
        if a.get("email") and not a["email"].endswith(_INTERNAL_DOMAIN)
    ]


def resolve_opportunity(emails: list[str]) -> dict[str, Any] | None:
    """Map external attendee emails → most recent open Opportunity."""
    if not emails:
        return None
    clause = "(" + ", ".join(f"'{e}'" for e in emails[:10]) + ")"
    q = (
        "SELECT OpportunityId, Opportunity.Name, Opportunity.StageName, "
        "Opportunity.Amount, Opportunity.CloseDate, Opportunity.IsClosed, "
        "Opportunity.AccountId, Opportunity.Account.Name, Opportunity.Account.Website, "
        "Opportunity.Owner.Email "
        "FROM OpportunityContactRole "
        f"WHERE Contact.Email IN {clause} "
        "AND Opportunity.IsClosed = false "
        "ORDER BY Opportunity.LastModifiedDate DESC"
    )
    try:
        rows = salesforce_mcp.soql_query(q, limit=5).get("records", []) or []
    except Exception as e:  # noqa: BLE001 — SOQL shouldn't blow up the trigger
        log.warning("resolve_opportunity failed for %s: %s", emails[:3], e)
        return None
    if not rows:
        return None
    first = rows[0]
    opp = first.get("Opportunity") or {}
    account = opp.get("Account") or {}
    return {
        "Id": first["OpportunityId"],
        "Name": opp.get("Name"),
        "StageName": opp.get("StageName"),
        "Amount": opp.get("Amount"),
        "CloseDate": opp.get("CloseDate"),
        "AccountId": opp.get("AccountId"),
        "AccountName": account.get("Name"),
        "Website": account.get("Website"),
        "OwnerEmail": ((opp.get("Owner") or {}).get("Email") or "").lower() or None,
    }


def scan_upcoming(*, min_lookahead_minutes: int = 90, lookahead_minutes: int = 120) -> list[dict[str, Any]]:
    """Return normalized demo-candidates with resolved Opp attached.

    Output shape per entry:
      { event: {...gcal event...}, opportunity: {...sf opp...} | None }
    """
    try:
        events = gcal.list_upcoming(
            lookahead_minutes=lookahead_minutes,
            min_lookahead_minutes=min_lookahead_minutes,
        )
    except Exception as e:  # noqa: BLE001 — GCal down → no briefs, log and move on
        log.exception("scan_upcoming: gcal fetch failed")
        return [{"error": str(e)}]

    out: list[dict[str, Any]] = []
    for event in events:
        if not is_demo(event):
            continue
        emails = external_attendees(event)
        opp = resolve_opportunity(emails)
        out.append({
            "event": event,
            "external_emails": emails,
            "opportunity": opp,
        })
    return out
