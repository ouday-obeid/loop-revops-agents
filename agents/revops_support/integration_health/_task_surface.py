"""Shared helper for monitors: idempotently surface a task for revops_support.

Every integration_health monitor converges on this single helper so the
deduplication key (`tasks.source`) is unambiguous and we never spam the queue
with the same probe failure twice.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine


def surface_task(
    *,
    source: str,
    title: str,
    description: str,
    category: str,
    priority: str = "medium",
    metadata: dict[str, Any] | None = None,
) -> int | None:
    """Create a task if no open one exists for `source`; return its id (or None).

    Matches the pattern agents/cs/integration_health.py uses so operators
    see a consistent task shape regardless of which monitor surfaced it.
    """
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                "SELECT id FROM tasks WHERE source = :s AND status != 'completed' LIMIT 1"
            ),
            {"s": source},
        ).fetchone()
        if existing:
            return int(existing[0])
        result = conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('revops_support', :t, :d, 'pending', :p,
                           :c, :s, 'system', :m)"""
            ),
            {
                "t": title,
                "d": description,
                "p": priority,
                "c": category,
                "s": source,
                "m": json.dumps(metadata or {}),
            },
        )
        task_id = result.lastrowid
        if task_id is None:
            row = conn.execute(
                text("SELECT id FROM tasks ORDER BY id DESC LIMIT 1")
            ).fetchone()
            task_id = row[0] if row else None
        return int(task_id) if task_id is not None else None
