"""Validation Rule health monitor.

Read-only surface over every active `ValidationRule` in the org:
  - fetches rules via Tooling API (grouped by object, enriched with
    LastModifiedBy name + LastModifiedDate)
  - flags orphaned rules (formula references a custom field that no longer
    exists on the object) — **gated on SF perm**; see note below
  - flags stale rules (not modified in ``stale_days`` — default 540 ≈ 18mo)
  - writes a ``tasks`` row for each flagged rule so Duncan can review

No Salesforce writes, no approval gate. Runs against the read alias.

Perm note: ``ErrorConditionFormula`` on ``ValidationRule`` requires the
"View Setup and Configuration" system permission on the running user.
Loop's ``salesops@tryloop.ai`` does not have it, so the field is dropped
from the Tooling query and ``detect_orphans()`` degrades to a no-op when
no formula text is available. Stale detection + rule inventory work
without that perm.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import write_audit
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

DEFAULT_STALE_DAYS = 540
CUSTOM_FIELD_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*__c)\b"
)

_RULE_QUERY = (
    "SELECT Id, ValidationName, Active, ErrorMessage, Description, "
    "EntityDefinition.QualifiedApiName, "
    "LastModifiedDate, LastModifiedBy.Name "
    "FROM ValidationRule WHERE Active = true"
)


def fetch_active_rules(
    *,
    tooling_query: Callable[..., dict[str, Any]] = salesforce_mcp.tooling_query,
) -> list[dict[str, Any]]:
    """Pull every active ValidationRule across the org via Tooling API."""
    result = tooling_query(_RULE_QUERY, limit=2000)
    out: list[dict[str, Any]] = []
    for rec in result.get("records", []):
        entity = (rec.get("EntityDefinition") or {}).get("QualifiedApiName")
        owner = (rec.get("LastModifiedBy") or {}).get("Name")
        out.append(
            {
                "id": rec.get("Id"),
                "name": rec.get("ValidationName"),
                "object": entity,
                "active": rec.get("Active"),
                "error_message": rec.get("ErrorMessage"),
                "description": rec.get("Description"),
                "formula": rec.get("ErrorConditionFormula") or "",
                "last_modified": rec.get("LastModifiedDate"),
                "owner": owner,
            }
        )
    return out


def _referenced_custom_fields(formula: str) -> set[str]:
    """Extract top-level __c field tokens from an ErrorConditionFormula.

    We only match ``*__c`` identifiers — standard fields are rarely renamed and
    including them would produce false positives against SF function names.
    Cross-object field paths like ``Account.Foo__c`` have both segments
    returned so the caller can validate either the lookup or the leaf field.
    """
    return {m.group(1) for m in CUSTOM_FIELD_RE.finditer(formula or "")}


def detect_orphans(
    rules: list[dict[str, Any]],
    *,
    describe_fn: Callable[[str], dict[str, Any]] = salesforce_mcp.describe_sobject,
) -> list[dict[str, Any]]:
    """Return rules whose formula references a __c field missing from describe.

    Degrades to an empty list when no rule has formula text (the Tooling query
    skips ``ErrorConditionFormula`` under the current perm set). A single WARN
    makes the degradation visible without fanning out to per-rule noise.
    """
    if rules and not any((rule.get("formula") or "").strip() for rule in rules):
        log.warning(
            "detect_orphans: no formula text available on any of %d rules — "
            "orphan detection skipped (ErrorConditionFormula perm-gated)",
            len(rules),
        )
        return []
    field_cache: dict[str, set[str]] = {}
    orphans: list[dict[str, Any]] = []
    for rule in rules:
        obj = rule.get("object")
        if not obj:
            continue
        referenced = _referenced_custom_fields(rule.get("formula", ""))
        if not referenced:
            continue
        if obj not in field_cache:
            try:
                describe = describe_fn(obj)
            except Exception as e:  # noqa: BLE001 — isolate per-object failure
                log.warning("describe failed for %s: %s", obj, e)
                field_cache[obj] = set()
            else:
                fields = describe.get("fields") or []
                field_cache[obj] = {f.get("name") for f in fields if f.get("name")}
        known = field_cache[obj]
        # Cross-object refs: only validate the head of the path. If the leading
        # segment is itself a __c lookup on the object, mismatch is reported.
        missing = {
            ref for ref in referenced if ref.split(".")[0] not in known
        }
        if missing:
            orphans.append(
                {
                    **rule,
                    "issue": "orphaned_field_reference",
                    "missing_fields": sorted(missing),
                }
            )
    return orphans


def detect_stale(
    rules: list[dict[str, Any]],
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return rules whose LastModifiedDate is older than ``stale_days``."""
    now_dt = now or datetime.now(timezone.utc)
    stale: list[dict[str, Any]] = []
    for rule in rules:
        ts = rule.get("last_modified")
        if not ts:
            continue
        try:
            parsed = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        age_days = (now_dt - parsed).days
        if age_days >= stale_days:
            stale.append(
                {
                    **rule,
                    "issue": "stale",
                    "age_days": age_days,
                }
            )
    return stale


def summarize(rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by object and by owner."""
    by_object: dict[str, int] = {}
    by_owner: dict[str, int] = {}
    for rule in rules:
        by_object[rule.get("object") or "<unknown>"] = (
            by_object.get(rule.get("object") or "<unknown>", 0) + 1
        )
        by_owner[rule.get("owner") or "<unknown>"] = (
            by_owner.get(rule.get("owner") or "<unknown>", 0) + 1
        )
    return {
        "total": len(rules),
        "by_object": by_object,
        "by_owner": by_owner,
    }


def _task_for_review(
    flagged: Iterable[dict[str, Any]],
    *,
    assignee: str = "duncan",
    agent_name: str = "revops_support",
) -> list[int]:
    """Insert a `tasks` row for each flagged rule, skipping duplicates.

    Deduplication key: agent_name + title. Matches an existing pending row by
    title so a repeated poll doesn't generate a wall of duplicate tasks.
    """
    flagged = list(flagged)
    if not flagged:
        return []
    now = datetime.now(timezone.utc)
    created: list[int] = []
    with get_engine().begin() as conn:
        for rule in flagged:
            title = f"ValidationRule review: {rule.get('object')}.{rule.get('name')} — {rule.get('issue')}"
            existing = conn.execute(
                text(
                    "SELECT id FROM tasks WHERE agent_name = :agent AND title = :title "
                    "AND status = 'pending' LIMIT 1"
                ),
                {"agent": agent_name, "title": title},
            ).fetchone()
            if existing:
                continue
            payload = {
                "rule_id": rule.get("id"),
                "object": rule.get("object"),
                "issue": rule.get("issue"),
                "missing_fields": rule.get("missing_fields"),
                "age_days": rule.get("age_days"),
                "owner": rule.get("owner"),
            }
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
                    "desc": rule.get("description") or rule.get("error_message"),
                    "prio": "high" if rule.get("issue") == "orphaned_field_reference" else "medium",
                    "cat": "validation_rule_review",
                    "assignee": assignee,
                    "now": now,
                    "meta": json.dumps(payload),
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
    stale_days: int = DEFAULT_STALE_DAYS,
    tooling_query: Callable[..., dict[str, Any]] = salesforce_mcp.tooling_query,
    describe_fn: Callable[[str], dict[str, Any]] = salesforce_mcp.describe_sobject,
    assignee: str = "duncan",
    agent_name: str = "revops_support",
) -> dict[str, Any]:
    """One-shot monitor. Returns summary + flagged rules + task ids created."""
    rules = fetch_active_rules(tooling_query=tooling_query)
    orphans = detect_orphans(rules, describe_fn=describe_fn)
    stale = detect_stale(rules, stale_days=stale_days)

    flagged: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for rule in orphans + stale:
        key = f"{rule.get('id')}::{rule.get('issue')}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        flagged.append(rule)

    task_ids = _task_for_review(flagged, assignee=assignee, agent_name=agent_name)

    write_audit(
        agent_name=agent_name,
        action="validation_monitor_poll",
        target="sf:ValidationRule",
        after={
            "total_active": len(rules),
            "orphaned": len(orphans),
            "stale": len(stale),
            "tasks_created": len(task_ids),
        },
    )

    return {
        "summary": summarize(rules),
        "orphans": orphans,
        "stale": stale,
        "flagged": flagged,
        "task_ids": task_ids,
    }
