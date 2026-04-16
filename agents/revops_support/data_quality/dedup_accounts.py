"""Account deduplication — name+domain fuzzy cluster, propose merges, execute.

Safer than Contact dedup: Account merges collapse TLO rollups and children
(Contacts, Opps, Cases) into the master. Clustering is deliberately
conservative:

  1. Normalize Name (strip common suffixes, lowercase, alphanumeric-only)
  2. Extract domain from Website
  3. Two Accounts cluster together only if `normalized_name` matches AND
     (`domain` matches OR both share `BillingState`+`BillingCity`).

Master pick: most opportunities > most recent activity > oldest CreatedDate.

Approval tier: ``account_merge`` (dual_approval, o_or_dept_head). Same
SF 2-per-call REST limit as Contact merges → clusters >3 split.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import write_audit
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"
TASK_CATEGORY = "account_dedup_review"
SF_MERGE_BATCH = 2

_ACCOUNT_QUERY = (
    "SELECT Id, Name, Website, BillingCity, BillingState, BillingCountry, "
    "CreatedDate, LastActivityDate, OwnerId, "
    "(SELECT Id FROM Opportunities) "
    "FROM Account "
)

# Suffixes/prefixes we strip to normalize company names before clustering.
_NAME_NOISE = re.compile(
    r"\b(inc|llc|ltd|co|corp|corporation|holdings|group|company|the|"
    r"limited|llp|plc|lp)\b\.?",
    re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    cleaned = _NAME_NOISE.sub(" ", name.lower())
    return _NON_ALNUM.sub("", cleaned)


def extract_domain(website: str | None) -> str | None:
    if not website:
        return None
    w = website.strip().lower()
    w = re.sub(r"^https?://", "", w)
    w = re.sub(r"^www\.", "", w)
    w = w.split("/")[0].split("?")[0].strip()
    return w or None


def scan_accounts(
    *,
    soql_query: Callable[..., dict[str, Any]] = salesforce_mcp.soql_query,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Return every Account with normalized name + domain + opp count."""
    result = soql_query(_ACCOUNT_QUERY, limit=limit)
    out: list[dict[str, Any]] = []
    for rec in result.get("records", []):
        opps = rec.get("Opportunities") or {}
        opp_records = opps.get("records") or []
        out.append(
            {
                "id": rec.get("Id"),
                "name": rec.get("Name"),
                "normalized_name": normalize_name(rec.get("Name")),
                "domain": extract_domain(rec.get("Website")),
                "city": rec.get("BillingCity"),
                "state": rec.get("BillingState"),
                "country": rec.get("BillingCountry"),
                "owner_id": rec.get("OwnerId"),
                "created_date": rec.get("CreatedDate"),
                "last_activity_date": rec.get("LastActivityDate"),
                "opp_count": len(opp_records),
            }
        )
    return out


def cluster_accounts(
    accounts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return clusters where >1 account shares name AND (domain | city+state)."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for acct in accounts:
        key = acct["normalized_name"]
        if not key:
            continue
        by_name.setdefault(key, []).append(acct)

    clusters: list[dict[str, Any]] = []
    for normalized_name, group in by_name.items():
        if len(group) < 2:
            continue

        # Sub-cluster within the same normalized name by a secondary signal.
        subs: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for a in group:
            secondary = (a["domain"] or "", (a["city"] or "").lower(),
                         (a["state"] or "").lower())
            subs.setdefault(secondary, []).append(a)

        # Merge sub-clusters that share ANY of: domain, (city, state) pair.
        merged: list[list[dict[str, Any]]] = []
        for (dom, city, st), members in subs.items():
            placed = False
            for existing in merged:
                if any(
                    (m["domain"] and m["domain"] == dom)
                    or ((m["city"] or "").lower() == city
                        and (m["state"] or "").lower() == st
                        and city)
                    for m in existing
                ):
                    existing.extend(members)
                    placed = True
                    break
            if not placed:
                merged.append(list(members))

        for cluster in merged:
            if len(cluster) > 1:
                clusters.append(
                    {"normalized_name": normalized_name, "accounts": cluster}
                )
    return clusters


def _score_master_candidate(acct: dict[str, Any]) -> tuple:
    return (
        acct.get("opp_count") or 0,
        acct.get("last_activity_date") or "",
        -_iso_sortkey(acct.get("created_date") or ""),
    )


def _iso_sortkey(ts: str) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def propose_merges(clusters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    for cluster in clusters:
        accounts = cluster.get("accounts") or []
        if len(accounts) < 2:
            continue
        ranked = sorted(accounts, key=_score_master_candidate, reverse=True)
        master = ranked[0]
        duplicates = ranked[1:]
        proposals.append(
            {
                "normalized_name": cluster.get("normalized_name"),
                "master_id": master.get("id"),
                "duplicate_ids": [d.get("id") for d in duplicates],
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
) -> list[dict[str, Any]]:
    master_id = proposal["master_id"]
    duplicate_ids: list[str] = list(proposal["duplicate_ids"])
    results: list[dict[str, Any]] = []
    for i in range(0, len(duplicate_ids), SF_MERGE_BATCH):
        chunk = duplicate_ids[i : i + SF_MERGE_BATCH]
        res = merge_fn(
            "Account",
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
                f"Account merge review: {p.get('master', {}).get('name')} "
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
                        f"Master: {p.get('master', {}).get('id')} "
                        f"(opps={p.get('master', {}).get('opp_count', 0)}); "
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
    accounts = scan_accounts(soql_query=soql_query)
    clusters = cluster_accounts(accounts)
    proposals = propose_merges(clusters)
    task_ids = _task_for_review(proposals, assignee=assignee)

    merges: list[dict[str, Any]] = []
    if repair_ and proposals:
        if approval_gate_id is None:
            raise ValueError("repair_=True requires approval_gate_id")
        for p in proposals:
            merges.extend(
                apply_merge(p, approval_gate_id=approval_gate_id, merge_fn=merge_fn)
            )

    write_audit(
        agent_name=AGENT_NAME,
        action="dedup_accounts_poll",
        target="sf:Account",
        after={
            "accounts_scanned": len(accounts),
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
