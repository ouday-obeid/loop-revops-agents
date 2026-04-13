"""Decision-maker enrichment for pre-demo briefs.

Wraps Clay. Input: company domain + attendee emails. Output: ranked list
of people the AE should know about going into the call — known attendees
first, then other buying-committee members by title tier.
"""
from __future__ import annotations

import logging
from typing import Any

from agents.sales_reps.integrations import clay

log = logging.getLogger(__name__)

_MAX_ATTENDEE_LOOKUPS = 5
_MAX_COMMITTEE_ADDS = 5


def enrich_for_brief(
    domain: str | None,
    attendee_emails: list[str],
) -> list[dict[str, Any]]:
    """Build the 'people to know' list for a brief.

    Strategy:
      1. Enrich each known attendee email via Clay (up to 5).
      2. Supplement with decision-makers at the domain (up to 5) that are
         not already in the attendee set.
      3. Return the merged list — attendees first, then committee.
    """
    attendees_out: list[dict[str, Any]] = []
    seen_emails: set[str] = set()

    for email in (attendee_emails or [])[:_MAX_ATTENDEE_LOOKUPS]:
        try:
            person = clay.enrich_contact(email)
        except clay.ClayError as e:
            log.warning("clay enrich skipped for %s: %s", email, e)
            person = None
        if person:
            attendees_out.append({**person, "attending": True})
            if person.get("email"):
                seen_emails.add(person["email"])
        else:
            # Fall back to a minimal record so the brief still shows the attendee.
            attendees_out.append({
                "name": None, "email": email.lower(), "title": None,
                "linkedin_url": None, "source": "attendee_only", "attending": True,
            })
            seen_emails.add(email.lower())

    committee: list[dict[str, Any]] = []
    if domain:
        try:
            committee_raw = clay.find_decision_makers(domain, limit=_MAX_COMMITTEE_ADDS * 2)
        except clay.ClayError as e:
            log.warning("clay committee lookup failed for %s: %s", domain, e)
            committee_raw = []
        for p in committee_raw:
            if p.get("email") and p["email"] in seen_emails:
                continue
            committee.append({**p, "attending": False})
            if len(committee) >= _MAX_COMMITTEE_ADDS:
                break

    return attendees_out + committee
