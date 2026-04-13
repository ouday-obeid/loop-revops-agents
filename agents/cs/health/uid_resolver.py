"""Deterministic SF Account.Id ↔ Vitally UID resolver.

Per O (2026-04-13): UID is deterministic — no override file. Vitally stores the
SF Account.Id in Account.externalId. A mismatch is a data-quality issue for
RevOps Support, not a paper-over-with-yaml situation.

Functions here:
  - resolve(vitally_account) -> sf_account_id | None
  - log_miss(vitally_account, reason) -> creates a revops_support task + degrades
    integration_health for vitally_uid_resolution.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)

TASK_TITLE_PREFIX = "Vitally UID unresolvable"


def resolve(vitally_account: dict[str, Any]) -> str | None:
    """Return SF Account.Id if Vitally externalId is present and non-empty."""
    ext = vitally_account.get("externalId") or vitally_account.get("external_id")
    if not ext:
        return None
    ext = str(ext).strip()
    return ext or None


def log_miss(vitally_account: dict[str, Any], *, reason: str = "externalId missing") -> None:
    """Record a UID-resolution miss: create a revops_support task (idempotent by source)."""
    vitally_id = str(vitally_account.get("id") or "")
    vitally_name = str(vitally_account.get("name") or "(unnamed)")
    source = f"cs:uid_resolver:{vitally_id}"
    title = f"{TASK_TITLE_PREFIX}: {vitally_name}"
    description = (
        f"Vitally account `{vitally_id}` ({vitally_name}) has no externalId linking "
        f"it to an SF Account. Reason: {reason}. Resolution: link via Vitally's SF "
        "integration or set externalId manually to the SF Account.Id."
    )
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s AND status != 'completed' LIMIT 1"),
            {"s": source},
        ).fetchone()
        if exists:
            return
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('revops_support', :t, :d, 'pending', 'medium',
                           'sf_data_quality', :s, 'system', :m)"""
            ),
            {
                "t": title,
                "d": description,
                "s": source,
                "m": json.dumps({"vitally_id": vitally_id, "reason": reason}),
            },
        )
    log.info("uid_resolver miss recorded: %s", source)


def record_match_rate(total: int, matched: int) -> None:
    """Stamp integration_health with UID match-rate outcome."""
    if total == 0:
        return
    rate = matched / total
    status = "healthy" if rate >= 0.95 else "degraded"
    err = None if status == "healthy" else f"UID match rate {rate:.1%} below 95% threshold"
    now = datetime.now(timezone.utc)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO integration_health
                   (integration, status, last_success, last_failure, error_message, checked_at)
                   VALUES ('vitally_uid_resolution', :s, :ls, :lf, :e, :now)"""
            ),
            {
                "s": status,
                "ls": now if status == "healthy" else None,
                "lf": now if status != "healthy" else None,
                "e": err,
                "now": now,
            },
        )
