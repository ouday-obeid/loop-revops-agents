"""Contact deduplication — cluster by email, propose merges, execute on approval.

Flow:
    scan_clusters() -> propose_merges() -> apply(cluster, gate_id) -> tasks

Approval tier: ``contact_merge`` (dual_approval, o_or_dept_head). SF REST
``merge`` endpoint limits each call to 2 duplicates → clusters of size > 3
are split into multiple gated calls on the same master.

``poll`` is scan-only by default. Pass ``repair=True`` + an approved gate to
execute the proposed merges; otherwise it creates pending Duncan tasks.
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
TASK_CATEGORY = "contact_dedup_review"
SF_MERGE_BATCH = 2  # SF REST merge duplicates-per-call limit

_CLUSTER_QUERY = (
    "SELECT Id, Name, Email, AccountId, OwnerId, CreatedDate, LastActivityDate, "
    "LastModifiedDate, Account.Name "
    "FROM Contact WHERE Email != null "
    "AND Email IN ({emails})"
)

_EMAILS_QUERY = (
    "SELECT Email, COUNT(Id) c FROM Contact "
    "WHERE Email != null GROUP BY Email HAVING COUNT(Id) > 1"
)


def _fetch_duplicate_emails(
    *,
    soql_query: Callable[..., dict[str, Any]] = salesforce_mcp.soql_query,
    limit: int = 2000,
) -> list[str]:
    result = soql_query(_EMAILS_QUERY, limit=limit)
    emails: list[str] = []
    for row in result.get("records", []):
        email = row.get("Email")
        if email:
            emails.append(email)
    return emails


def scan_clusters(
    *,
    soql_query: Callable[..., dict[str, Any]] = salesforce_mcp.soql_query,
    max_emails: int = 500,
) -> list[dict[str, Any]]:
    """Return clusters: [{"email": ..., "contacts": [...]}, ...].

    Two SOQL hops: first a GROUP BY to find duplicate emails, then a detail
    fetch for those specific emails. The second query is bounded by
    ``max_emails`` to cap SOQL IN-clause length.
    """
    emails = _fetch_duplicate_emails(soql_query=soql_query)[:max_emails]
    if not emails:
        return []
    quoted = ", ".join(f"'{e.replace(chr(39), chr(92) + chr(39))}'" for e in emails)
    result = soql_query(_CLUSTER_QUERY.format(emails=quoted), limit=2000)

    by_email: dict[str, list[dict[str, Any]]] = {}
    for rec in result.get("records", []):
        email = rec.get("Email")
        if not email:
            continue
        by_email.setdefault(email, []).append(
            {
                "id": rec.get("Id"),
                "name": rec.get("Name"),
                "email": email,
                "account_id": rec.get("AccountId"),
                "account_name": (rec.get("Account") or {}).get("Name"),
                "owner_id": rec.get("OwnerId"),
                "created_date": rec.get("CreatedDate"),
                "last_activity_date": rec.get("LastActivityDate"),
                "last_modified_date": rec.get("LastModifiedDate"),
            }
        )

    return [
        {"email": e, "contacts": contacts}
        for e, contacts in sorted(by_email.items())
        if len(contacts) > 1
    ]


def _score_master_candidate(contact: dict[str, Any]) -> tuple:
    """Higher is better. Lexicographic tuple picks the master."""
    has_account = 1 if contact.get("account_id") else 0
    last_activity = contact.get("last_activity_date") or ""
    created = contact.get("created_date") or ""
    # Prefer has_account, then most recent activity, then EARLIEST created.
    return (has_account, last_activity, -_iso_sortkey(created))


def _iso_sortkey(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def propose_merges(
    clusters: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """For each cluster produce {"email", "master_id", "duplicate_ids", "master", "duplicates"}."""
    proposals: list[dict[str, Any]] = []
    for cluster in clusters:
        contacts = cluster.get("contacts") or []
        if len(contacts) < 2:
            continue
        ranked = sorted(contacts, key=_score_master_candidate, reverse=True)
        master = ranked[0]
        duplicates = ranked[1:]
        proposals.append(
            {
                "email": cluster.get("email"),
                "master_id": master.get("id"),
                "duplicate_ids": [c.get("id") for c in duplicates],
                "master": master,
                "duplicates": duplicates,
            }
        )
    return proposals


def apply_merge(
    proposal: dict[str, Any],
    *,
    approval_gate_id: int,
    merge_fn: Callable[..., dict[str, Any]] = salesforce_mcp.merge_records,
    sobject: str = "Contact",
) -> list[dict[str, Any]]:
    """Execute merges for a single proposal, batching SF's 2-per-call cap."""
    master_id = proposal["master_id"]
    duplicate_ids: list[str] = list(proposal["duplicate_ids"])
    results: list[dict[str, Any]] = []
    for i in range(0, len(duplicate_ids), SF_MERGE_BATCH):
        chunk = duplicate_ids[i : i + SF_MERGE_BATCH]
        res = merge_fn(
            sobject,
            master_id,
            chunk,
            agent_name=AGENT_NAME,
            approval_gate_id=approval_gate_id,
        )
        results.append({"chunk": chunk, "result": res})
    return results


def _task_for_review(
    proposals: list[dict[str, Any]],
    *,
    assignee: str = "duncan",
) -> list[int]:
    if not proposals:
        return []
    now = datetime.now(timezone.utc)
    created: list[int] = []
    with get_engine().begin() as conn:
        for p in proposals:
            title = (
                f"Contact merge review: {p.get('email')} "
                f"({len(p.get('duplicate_ids', []))} dupes)"
            )
            existing = conn.execute(
                text(
                    "SELECT id FROM tasks WHERE agent_name = :a AND title = :t "
                    "AND status = 'pending' LIMIT 1"
                ),
                {"a": AGENT_NAME, "t": title},
            ).fetchone()
            if existing:
                continue
            result = conn.execute(
                text(
                    "INSERT INTO tasks (agent_name, title, description, status, priority, "
                    "category, assignee, created_at, updated_at, metadata) "
                    "VALUES (:a, :t, :d, 'pending', 'high', :c, :as, :n, :n, :m)"
                ),
                {
                    "a": AGENT_NAME,
                    "t": title,
                    "d": (
                        f"Master: {p.get('master', {}).get('name')} ({p.get('master_id')}); "
                        f"Duplicates: {', '.join(p.get('duplicate_ids', []))}"
                    ),
                    "c": TASK_CATEGORY,
                    "as": assignee,
                    "n": now,
                    "m": json.dumps(p),
                },
            )
            tid = result.lastrowid
            if tid is None:
                tid = conn.execute(
                    text("SELECT id FROM tasks ORDER BY id DESC LIMIT 1")
                ).fetchone()[0]
            created.append(int(tid))
    return created


def poll(
    *,
    repair_: bool = False,
    approval_gate_id: int | None = None,
    assignee: str = "duncan",
    soql_query: Callable[..., dict[str, Any]] = salesforce_mcp.soql_query,
    merge_fn: Callable[..., dict[str, Any]] = salesforce_mcp.merge_records,
) -> dict[str, Any]:
    """Scan → propose → task, and optionally apply merges."""
    clusters = scan_clusters(soql_query=soql_query)
    proposals = propose_merges(clusters)
    task_ids = _task_for_review(proposals, assignee=assignee)

    merges: list[dict[str, Any]] = []
    if repair_ and proposals:
        if approval_gate_id is None:
            raise ValueError("repair_=True requires approval_gate_id")
        for p in proposals:
            merges.extend(
                apply_merge(
                    p, approval_gate_id=approval_gate_id, merge_fn=merge_fn
                )
            )

    write_audit(
        agent_name=AGENT_NAME,
        action="dedup_contacts_poll",
        target="sf:Contact",
        after={
            "clusters": len(clusters),
            "proposals": len(proposals),
            "merges_executed": len(merges),
            "tasks_created": len(task_ids),
        },
        approval_gate_id=approval_gate_id,
    )

    return {
        "clusters": clusters,
        "proposals": proposals,
        "task_ids": task_ids,
        "merges": merges,
    }
