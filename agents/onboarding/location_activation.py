"""Location activation tracker — classifies stuck `Location__c` by reason.

Daily 9 AM sweep (see schedule.py). Groups stuck locations by
`Stuck_Reason__c` picklist value and produces a weekly summary for Jackie.

Field-name resilience: `Stuck_Reason__c` and `Activation_Status__c` are the
names Appendix C of the scoping doc uses, but the KB notes that account-side
admin work sometimes renames or repurposes these. At first use the module
calls `describe_sobject('Location__c')` and verifies both names exist. If
either is missing, it falls back to the closest match (anything containing
`Status` / `Stuck`) and logs a WARNING so Agent 5 can repair the schema.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any

from agents.onboarding import queries
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)


_FIELD_CACHE: dict[str, str | None] = {}


def _resolved_field(candidate: str) -> str | None:
    """Check Location__c has a field; return the verified name or a fallback.

    Caches the result per process — the schema does not change mid-run.
    """
    if candidate in _FIELD_CACHE:
        return _FIELD_CACHE[candidate]
    try:
        desc = salesforce_mcp.describe_sobject("Location__c")
    except Exception:
        log.warning("describe_sobject(Location__c) failed; using %s as-is", candidate,
                    exc_info=True)
        _FIELD_CACHE[candidate] = candidate
        return candidate

    names = {f.get("name") for f in (desc.get("fields") or []) if f.get("name")}
    if candidate in names:
        _FIELD_CACHE[candidate] = candidate
        return candidate

    # Heuristic fallback — longest-prefix match on the lowercased root.
    root = candidate.replace("__c", "").lower()
    fuzzy = [n for n in names if root in n.lower() or (root[:5] and root[:5] in n.lower())]
    fallback = fuzzy[0] if fuzzy else None
    if fallback:
        log.warning(
            "Location__c missing %s; falling back to %s — flag for Agent 5",
            candidate, fallback,
        )
    else:
        log.warning(
            "Location__c has neither %s nor a fuzzy match; location sweep skipped",
            candidate,
        )
    _FIELD_CACHE[candidate] = fallback
    return fallback


# ---------- Public entry points ----------

async def sweep() -> dict[str, Any]:
    """Scheduled daily sweep — summarizes stuck locations by reason.

    Returns a summary dict the cron runner can log / surface.
    """
    if _resolved_field("Activation_Status__c") is None:
        log.warning("no Activation_Status__c-like field on Location__c; skipping sweep")
        return {"skipped": True, "reason": "schema_gap"}

    res = salesforce_mcp.soql_query(queries.STUCK_LOCATIONS_ALL)
    rows = res.get("records") or []

    reason_counts: Counter[str] = Counter()
    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        reason = row.get("Stuck_Reason__c") or "(no reason set)"
        reason_counts[reason] += 1
        account_name = ((row.get("Account__r") or {}).get("Name")
                        or row.get("Account__c") or "(no account)")
        by_account[account_name].append(row)

    summary = {
        "total_stuck": len(rows),
        "by_reason": dict(reason_counts),
        "accounts_with_stuck": len(by_account),
    }
    log.info("location sweep: %s", summary)
    return summary


async def report(*, account_filter: str | None = None) -> str:
    """Return a formatted Slack string for `@oo onboarding stuck-locations`."""
    res = salesforce_mcp.soql_query(queries.STUCK_LOCATIONS_ALL)
    rows = res.get("records") or []
    if account_filter:
        needle = account_filter.lower()
        rows = [
            r for r in rows
            if needle in ((r.get("Account__r") or {}).get("Name", "")).lower()
        ]

    if not rows:
        return (
            f"No stuck locations for `{account_filter}`."
            if account_filter
            else "No stuck locations across accounts."
        )

    by_account: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        account = (row.get("Account__r") or {}).get("Name") or "(no account)"
        by_account[account].append(row)

    lines: list[str] = []
    for account in sorted(by_account):
        lines.append(f"*{account}* — {len(by_account[account])} stuck:")
        for loc in by_account[account][:5]:
            reason = loc.get("Stuck_Reason__c") or "(no reason)"
            status = loc.get("Activation_Status__c") or "(unknown)"
            lines.append(f"  • `{loc.get('Name', '—')}` — {status} / {reason}")
        if len(by_account[account]) > 5:
            lines.append(f"  …and {len(by_account[account]) - 5} more.")
    return "\n".join(lines)


# ---------- Top-reason classifier (exposed for tests / CLI) ----------

def classify(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[row.get("Stuck_Reason__c") or "(no reason set)"] += 1
    return dict(counts)
