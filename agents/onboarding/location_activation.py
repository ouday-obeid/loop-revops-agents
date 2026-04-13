"""Location activation tracker — classifies stuck `Location__c` by reason.

Daily 9 AM sweep (see schedule.py). Groups stuck locations by
`Stuck_Reason__c` picklist value and produces a weekly summary for Jackie.

Schema reality (verified against the `revagents` sandbox on 2026-04-13):
`Location__c` in Loop's org does NOT carry `Activation_Status__c` or
`Stuck_Reason__c` — only a boolean `Active__c` and a `TLO__c` lookup to
`Top_Level_Organization__c`. There is also no `Account__c` reference on
Location__c; Account association is derivable only through
`Opportunity__r.AccountId` on Onboarding__c.

Graceful degradation: on first sweep the module verifies field existence;
if the brief's named fields are absent it auto-seeds a task for Agent 5
(RevOps Support) and short-circuits to a `schema_gap` response. The sweep
stays a no-op until Agent 5 ships the schema change.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import text as sql_text

from agents.onboarding import queries
from shared.db.connection import get_engine
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

REQUIRED_FIELDS: tuple[str, ...] = ("Activation_Status__c", "Stuck_Reason__c")

_FIELD_CACHE: dict[str, bool] = {}


def _field_exists(name: str) -> bool:
    if name in _FIELD_CACHE:
        return _FIELD_CACHE[name]
    try:
        desc = salesforce_mcp.describe_sobject("Location__c")
    except Exception:
        log.warning("describe_sobject(Location__c) failed for %s", name, exc_info=True)
        _FIELD_CACHE[name] = False
        return False
    names = {f.get("name") for f in (desc.get("fields") or []) if f.get("name")}
    present = name in names
    _FIELD_CACHE[name] = present
    return present


def _seed_agent5_task(missing: list[str]) -> None:
    """Idempotently seed a RevOps Support task for the missing field(s)."""
    title = f"Add Location__c fields: {', '.join(missing)}"
    source = "onboarding:location_schema_gap"
    try:
        engine = get_engine()
        with engine.begin() as conn:
            existing = conn.execute(
                sql_text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"),
                {"s": source},
            ).fetchone()
            if existing:
                return
            conn.execute(
                sql_text(
                    """INSERT INTO tasks (agent_name, title, description, status,
                                          priority, category, source, assignee)
                       VALUES ('revops_support', :t, :d, 'pending', 'medium',
                               'sf_schema_add', :s, 'system')"""
                ),
                {
                    "t": title,
                    "d": (
                        "Onboarding agent detected missing fields on Location__c: "
                        + ", ".join(missing)
                        + ". These are required by the location activation sweep "
                        + "(daily 9 AM) and weekly Jackie digest. Payload: "
                        + json.dumps({"missing": missing, "sobject": "Location__c"})
                    ),
                    "s": source,
                },
            )
        log.info("seeded Agent 5 task for missing Location__c fields: %s", missing)
    except Exception:
        log.warning("failed to seed Agent 5 task for %s", missing, exc_info=True)


def _schema_gap() -> list[str]:
    """Return list of REQUIRED_FIELDS missing on Location__c."""
    return [f for f in REQUIRED_FIELDS if not _field_exists(f)]


# ---------- Public entry points ----------

async def sweep() -> dict[str, Any]:
    """Scheduled daily sweep — summarizes stuck locations by reason."""
    missing = _schema_gap()
    if missing:
        _seed_agent5_task(missing)
        log.warning("location sweep skipped — missing Location__c fields: %s", missing)
        return {"skipped": True, "reason": "schema_gap", "missing": missing}

    res = salesforce_mcp.soql_query(queries.INACTIVE_LOCATIONS_ALL)
    rows = res.get("records") or []

    reason_counts: Counter[str] = Counter()
    by_tlo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        reason = row.get("Stuck_Reason__c") or "(no reason set)"
        reason_counts[reason] += 1
        tlo_name = ((row.get("TLO__r") or {}).get("Name")
                    or row.get("TLO__c") or "(no TLO)")
        by_tlo[tlo_name].append(row)

    summary = {
        "total_inactive": len(rows),
        "by_reason": dict(reason_counts),
        "tlos_with_inactive": len(by_tlo),
    }
    log.info("location sweep: %s", summary)
    return summary


async def report(*, account_filter: str | None = None) -> str:
    """Return a formatted Slack string for `@oo onboarding stuck-locations`."""
    missing = _schema_gap()
    if missing:
        return (
            f":warning: Location activation data unavailable — missing fields "
            f"`{', '.join(missing)}` on `Location__c`. Task seeded for Agent 5."
        )

    res = salesforce_mcp.soql_query(queries.INACTIVE_LOCATIONS_ALL)
    rows = res.get("records") or []
    if account_filter:
        needle = account_filter.lower()
        rows = [
            r for r in rows
            if needle in ((r.get("TLO__r") or {}).get("Name", "")).lower()
        ]

    if not rows:
        return (
            f"No stuck locations for `{account_filter}`."
            if account_filter
            else "No stuck locations across organizations."
        )

    by_tlo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        tlo = (row.get("TLO__r") or {}).get("Name") or "(no TLO)"
        by_tlo[tlo].append(row)

    lines: list[str] = []
    for tlo in sorted(by_tlo):
        lines.append(f"*{tlo}* — {len(by_tlo[tlo])} stuck:")
        for loc in by_tlo[tlo][:5]:
            reason = loc.get("Stuck_Reason__c") or "(no reason)"
            status = loc.get("Activation_Status__c") or "(unknown)"
            lines.append(f"  • `{loc.get('Name', '—')}` — {status} / {reason}")
        if len(by_tlo[tlo]) > 5:
            lines.append(f"  …and {len(by_tlo[tlo]) - 5} more.")
    return "\n".join(lines)


# ---------- Top-reason classifier (exposed for tests / CLI) ----------

def classify(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in rows:
        counts[row.get("Stuck_Reason__c") or "(no reason set)"] += 1
    return dict(counts)
