"""Bad Lead-conversion detector.

Scans converted Leads for orphans:
  * no linked Opportunity (`ConvertedOpportunityId IS NULL`)
  * no linked Account (`ConvertedAccountId IS NULL`)
  * no linked Contact (`ConvertedContactId IS NULL`)

Each orphan gets a pending row in the `tasks` table for Duncan to triage.
Dedup-on-title keeps repeat polls idempotent. No SF writes in v1 — the
auto-repair path depends on Loop-specific Lead custom fields that Duncan
still needs to spec; `repair()` is intentionally wired but raises until
the `repair_field` policy is defined.

If the caller passes `approval_gate_id` and `repair_field`, `poll(repair=True,
...)` uses :class:`BulkUpdater` to prepend a DQ tag into that field as a
reversible, pre-snapshotted update. Without those, it behaves as a pure
detector.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import write_audit
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"
TASK_CATEGORY = "bad_conversion_review"

_ORPHAN_QUERY = (
    "SELECT Id, Name, Email, Company, ConvertedAccountId, ConvertedContactId, "
    "ConvertedOpportunityId, ConvertedDate, OwnerId "
    "FROM Lead WHERE IsConverted = true AND "
    "(ConvertedOpportunityId = null OR ConvertedAccountId = null "
    "OR ConvertedContactId = null)"
)


def scan(
    *,
    soql_query: Callable[..., dict[str, Any]] = salesforce_mcp.soql_query,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Return every converted Lead with at least one orphaned reference."""
    result = soql_query(_ORPHAN_QUERY, limit=limit)
    orphans: list[dict[str, Any]] = []
    for rec in result.get("records", []):
        issues: list[str] = []
        if not rec.get("ConvertedOpportunityId"):
            issues.append("no_opportunity")
        if not rec.get("ConvertedAccountId"):
            issues.append("no_account")
        if not rec.get("ConvertedContactId"):
            issues.append("no_contact")
        if not issues:
            # Shouldn't happen given the WHERE clause, but guard anyway.
            continue
        orphans.append(
            {
                "lead_id": rec.get("Id"),
                "lead_name": rec.get("Name"),
                "email": rec.get("Email"),
                "company": rec.get("Company"),
                "account_id": rec.get("ConvertedAccountId"),
                "contact_id": rec.get("ConvertedContactId"),
                "opportunity_id": rec.get("ConvertedOpportunityId"),
                "converted_date": rec.get("ConvertedDate"),
                "owner_id": rec.get("OwnerId"),
                "issues": issues,
            }
        )
    return orphans


def _task_for_review(
    orphans: list[dict[str, Any]],
    *,
    assignee: str = "duncan",
    agent_name: str = AGENT_NAME,
) -> list[int]:
    if not orphans:
        return []
    now = datetime.now(timezone.utc)
    created: list[int] = []
    with get_engine().begin() as conn:
        for o in orphans:
            title = (
                f"Lead conversion review: {o.get('lead_name')} ({o.get('lead_id')}) "
                f"— {', '.join(o.get('issues', []))}"
            )
            existing = conn.execute(
                text(
                    "SELECT id FROM tasks WHERE agent_name = :agent AND title = :title "
                    "AND status = 'pending' LIMIT 1"
                ),
                {"agent": agent_name, "title": title},
            ).fetchone()
            if existing:
                continue
            priority = "high" if "no_account" in o.get("issues", []) else "medium"
            result = conn.execute(
                text(
                    "INSERT INTO tasks (agent_name, title, description, status, priority, "
                    "category, assignee, created_at, updated_at, metadata) "
                    "VALUES (:agent, :title, :desc, 'pending', :prio, :cat, :assignee, "
                    ":now, :now, :meta)"
                ),
                {
                    "agent": agent_name,
                    "title": title,
                    "desc": (
                        f"Converted {o.get('converted_date')}; "
                        f"issues: {', '.join(o.get('issues', []))}"
                    ),
                    "prio": priority,
                    "cat": TASK_CATEGORY,
                    "assignee": assignee,
                    "now": now,
                    "meta": json.dumps(o),
                },
            )
            tid = result.lastrowid
            if tid is None:
                tid = conn.execute(
                    text("SELECT id FROM tasks ORDER BY id DESC LIMIT 1")
                ).fetchone()[0]
            created.append(int(tid))
    return created


def repair(
    orphans: list[dict[str, Any]],
    *,
    repair_field: str | None = None,
    approval_gate_id: int | None = None,
    bulk_updater_cls=None,
) -> dict[str, Any]:
    """Best-effort auto-repair.

    v1: disabled by default. Callers must explicitly pass `repair_field` AND
    an approved `approval_gate_id` of tier `bulk_update_small` or `bulk_update_large`.
    When supplied, pre-pends a DQ tag onto `repair_field` for each orphan via
    :class:`BulkUpdater` (pre-write snapshot captured in audit_log).
    """
    if not repair_field:
        raise NotImplementedError(
            "bad_conversions.repair requires a Loop-specific repair_field "
            "(e.g. 'Description' or a custom __c). Duncan must spec this."
        )
    if approval_gate_id is None:
        raise ValueError("repair() requires approval_gate_id")

    # Late import — keeps unit-tests that only exercise detection light.
    from agents.revops_support.data_quality.bulk_updater import BulkUpdater

    updater = (bulk_updater_cls or BulkUpdater)(agent_name=AGENT_NAME)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updates = [
        {
            "Id": o["lead_id"],
            repair_field: (
                f"[DQ-REPAIR {now_iso}] "
                f"Orphaned conversion: {', '.join(o['issues'])}"
            ),
        }
        for o in orphans
    ]
    result = updater.run("Lead", updates, approval_gate_id=approval_gate_id)
    return result.to_summary()


def poll(
    *,
    repair_: bool = False,
    repair_field: str | None = None,
    approval_gate_id: int | None = None,
    assignee: str = "duncan",
    soql_query: Callable[..., dict[str, Any]] = salesforce_mcp.soql_query,
) -> dict[str, Any]:
    """Scan, task, and optionally repair."""
    orphans = scan(soql_query=soql_query)
    task_ids = _task_for_review(orphans, assignee=assignee)

    repair_summary: dict[str, Any] | None = None
    if repair_ and orphans:
        repair_summary = repair(
            orphans,
            repair_field=repair_field,
            approval_gate_id=approval_gate_id,
        )

    counts: dict[str, int] = {"no_opportunity": 0, "no_account": 0, "no_contact": 0}
    for o in orphans:
        for i in o["issues"]:
            counts[i] = counts.get(i, 0) + 1

    write_audit(
        agent_name=AGENT_NAME,
        action="bad_conversions_poll",
        target="sf:Lead",
        after={
            "total_orphans": len(orphans),
            "counts": counts,
            "tasks_created": len(task_ids),
            "repaired": bool(repair_summary),
        },
        approval_gate_id=approval_gate_id,
    )

    return {
        "total": len(orphans),
        "counts": counts,
        "orphans": orphans,
        "task_ids": task_ids,
        "repair_summary": repair_summary,
    }
