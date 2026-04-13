"""Record "knowledge gap" tasks for things the agent knows nothing about.

Only ever writes to the `tasks` table (category='knowledge_gap'). Never edits
canonical markdown — human-curated knowledge is write-once-by-human.

Two entrypoints:
  - record_gap(title, description, source): insert one gap task (dedupe).
  - scan_custom_objects(): live SF → list of custom SObjects → for each one
    without supporting knowledge chunks, record a gap.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.mcp import knowledge_mcp, salesforce_mcp

log = logging.getLogger(__name__)

KNOWLEDGE_GAP_CATEGORY = "knowledge_gap"
DEFAULT_CORPUS = "sf_admin"
DEFAULT_SOURCE = "gap_surfacer:scan"


@dataclass(frozen=True)
class GapRecord:
    task_id: int
    title: str
    created: bool  # True if inserted, False if already existed


def record_gap(
    title: str,
    description: str,
    *,
    source: str = DEFAULT_SOURCE,
    priority: str = "medium",
    agent_name: str = "revops_support",
) -> GapRecord:
    """Insert a knowledge_gap task, or return the existing open one.

    Dedupe key: (agent_name, title, category, source) across rows with
    status='pending'. A closed gap task does NOT suppress re-opening — if
    the gap resurfaces after being resolved, we want a fresh row.
    """
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                "SELECT id FROM tasks "
                "WHERE agent_name = :a AND title = :t AND category = :c "
                "AND source = :s AND status = 'pending' "
                "LIMIT 1"
            ),
            {"a": agent_name, "t": title, "c": KNOWLEDGE_GAP_CATEGORY, "s": source},
        ).fetchone()
        if existing:
            return GapRecord(task_id=int(existing[0]), title=title, created=False)

        result = conn.execute(
            text(
                "INSERT INTO tasks "
                "(agent_name, title, description, status, priority, category, source, assignee) "
                "VALUES (:a, :t, :d, 'pending', :p, :c, :s, 'system')"
            ),
            {
                "a": agent_name,
                "t": title,
                "d": description,
                "p": priority,
                "c": KNOWLEDGE_GAP_CATEGORY,
                "s": source,
            },
        )
        tid = result.lastrowid
        if tid is None:
            row = conn.execute(
                text(
                    "SELECT id FROM tasks WHERE agent_name = :a AND title = :t "
                    "AND source = :s ORDER BY id DESC LIMIT 1"
                ),
                {"a": agent_name, "t": title, "s": source},
            ).fetchone()
            tid = int(row[0]) if row else 0
        log.info("gap recorded: %s (task_id=%s)", title, tid)
        return GapRecord(task_id=int(tid), title=title, created=True)


def _object_has_knowledge(sobject: str, corpus: str = DEFAULT_CORPUS, min_hits: int = 1) -> bool:
    """Heuristic: does any chunk in the corpus mention this SObject?"""
    hits = knowledge_mcp.semantic_search(sobject, corpus=corpus, k=3)
    strong = [h for h in hits if sobject in (h.get("content") or "")]
    return len(strong) >= min_hits


def scan_custom_objects(
    *, corpus: str = DEFAULT_CORPUS, source: str = DEFAULT_SOURCE,
) -> list[GapRecord]:
    """List custom SObjects via SF, record a gap for any without knowledge coverage."""
    sobjects = salesforce_mcp._sf("sobject", "list") or {}
    all_objects = sobjects.get("result") or sobjects.get("sobjects") or []
    custom = [s.get("name", "") for s in all_objects if s.get("custom") and s.get("name")]

    gaps: list[GapRecord] = []
    for name in sorted(set(custom)):
        if _object_has_knowledge(name, corpus=corpus):
            continue
        rec = record_gap(
            title=f"Missing knowledge for custom SObject {name}",
            description=(
                f"SF describe reports custom SObject `{name}`, but no chunks in "
                f"corpus `{corpus}` mention it. Run a snapshot and merge to close."
            ),
            source=source,
        )
        gaps.append(rec)
    log.info("gap scan: %d gaps recorded (%d new)", len(gaps), sum(g.created for g in gaps))
    return gaps


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Surface knowledge gaps as tasks")
    ap.add_argument("--scan", action="store_true", help="Scan custom SObjects for missing coverage")
    ap.add_argument("--title", help="Record a specific gap with this title")
    ap.add_argument("--description", default="", help="Gap description (used with --title)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.title:
        rec = record_gap(args.title, args.description)
        print(f"task={rec.task_id} created={rec.created}")
    elif args.scan:
        gaps = scan_custom_objects()
        print(f"{len(gaps)} gaps recorded; {sum(g.created for g in gaps)} new")
    else:
        ap.print_help()
