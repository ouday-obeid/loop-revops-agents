"""AsyncApexJob health probe: failures in last 24h + queue depth.

Two signals:
  1. **Recent failures** — any AsyncApexJob with `Status='Failed'` in the last
     24h, grouped by ApexClass.Name. Groups avoid one task per retry.
  2. **Queue depth** — count of jobs in `Status IN ('Queued','Preparing','Processing')`.
     When depth > QUEUE_DEPTH_WARN, we open a single "queue backing up" task.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.mcp import salesforce_mcp

from ._task_surface import surface_task

log = logging.getLogger(__name__)

FAILURE_WINDOW_HOURS = 24
QUEUE_DEPTH_WARN = 100
TASK_CATEGORY = "sf_integration_health"


@dataclass
class ApexJobProblem:
    kind: str  # "failures" | "queue_depth"
    class_name: str | None
    count: int


def _cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def detect_failures(jobs: list[dict[str, Any]]) -> list[ApexJobProblem]:
    failed = [j for j in jobs if (j.get("Status") or "") == "Failed"]
    if not failed:
        return []
    counts: Counter[str] = Counter()
    for j in failed:
        name = (
            j.get("ApexClass", {}).get("Name")
            if isinstance(j.get("ApexClass"), dict)
            else j.get("ApexClassName") or j.get("MethodName") or "<unknown>"
        )
        counts[name] += 1
    return [
        ApexJobProblem(kind="failures", class_name=n, count=c)
        for n, c in counts.most_common()
    ]


def detect_queue_depth(jobs: list[dict[str, Any]]) -> ApexJobProblem | None:
    pending = [
        j for j in jobs
        if (j.get("Status") or "") in ("Queued", "Preparing", "Processing")
    ]
    if len(pending) >= QUEUE_DEPTH_WARN:
        return ApexJobProblem(
            kind="queue_depth", class_name=None, count=len(pending),
        )
    return None


def _fetch_jobs(tooling_query=None) -> list[dict[str, Any]]:
    q = (
        "SELECT Id, Status, JobType, ApexClassId, MethodName, ExtendedStatus, "
        "CompletedDate, CreatedDate "
        "FROM AsyncApexJob "
        f"WHERE CreatedDate > {_cutoff_iso(FAILURE_WINDOW_HOURS)}"
    )
    tq = tooling_query or salesforce_mcp.tooling_query
    r = tq(q)
    return r.get("records", [])


def poll(*, tooling_query=None) -> list[dict[str, Any]]:
    jobs = _fetch_jobs(tooling_query=tooling_query)
    problems: list[ApexJobProblem] = detect_failures(jobs)
    qp = detect_queue_depth(jobs)
    if qp:
        problems.append(qp)

    surfaced: list[dict[str, Any]] = []
    for p in problems:
        if p.kind == "failures":
            source = f"revops_support:apex_job:failures:{p.class_name}"
            title = f"Apex class {p.class_name!r} had {p.count} failed job(s) in last 24h"
            priority = "high" if p.count >= 5 else "medium"
        else:
            source = "revops_support:apex_job:queue_depth"
            title = f"AsyncApexJob queue depth is {p.count} (>= {QUEUE_DEPTH_WARN})"
            priority = "high"
        description = (
            f"Detected by revops_support apex_job_monitor. kind={p.kind}, "
            f"count={p.count}. Investigate via AsyncApexJob / ApexClass Tooling API."
        )
        task_id = surface_task(
            source=source,
            title=title,
            description=description,
            category=TASK_CATEGORY,
            priority=priority,
            metadata={"kind": p.kind, "class_name": p.class_name, "count": p.count},
        )
        surfaced.append({"task_id": task_id, **p.__dict__})

    log.info("apex_job_monitor: surfaced %d problem(s)", len(surfaced))
    return surfaced
