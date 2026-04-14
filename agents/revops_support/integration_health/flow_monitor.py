"""Salesforce Flow + FlowInterview health probe.

Two checks:
  1. **Inactive/Obsolete flows that should be active** — flow with
     `Status='Obsolete'` but there exist recent FlowInterview runs for the
     same DeveloperName → someone is still triggering a retired flow, which
     is a silent automation break.
  2. **FlowInterview failures in last 24h** — any FlowInterview with
     `InterviewStatus='Error'` is surfaced as a task. Groups by Flow to
     avoid one task per failed run.

Both queries hit the Tooling API via `shared.mcp.salesforce_mcp.tooling_query`.
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
TASK_CATEGORY = "sf_integration_health"


@dataclass
class FlowProblem:
    kind: str  # "obsolete_still_running" | "recent_failures"
    flow_name: str
    count: int
    sample_id: str | None = None


def _recent_cutoff_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def detect_failures(interviews: list[dict[str, Any]]) -> list[FlowProblem]:
    """Pure function: group FlowInterview rows by flow name, report >0 errors."""
    errors = [r for r in interviews if (r.get("InterviewStatus") or "").lower() == "error"]
    if not errors:
        return []
    counts: Counter[str] = Counter()
    sample: dict[str, str] = {}
    for r in errors:
        name = (
            r.get("FlowDefinitionView", {}).get("DeveloperName")
            if isinstance(r.get("FlowDefinitionView"), dict)
            else r.get("Flow__c") or r.get("InterviewLabel") or "<unknown>"
        )
        counts[name] += 1
        sample.setdefault(name, r.get("Id") or "")
    return [
        FlowProblem(
            kind="recent_failures",
            flow_name=name,
            count=cnt,
            sample_id=sample.get(name),
        )
        for name, cnt in counts.most_common()
    ]


def detect_obsolete_active(flows: list[dict[str, Any]]) -> list[FlowProblem]:
    """Flows flagged Obsolete but DeveloperName still firing interviews."""
    out: list[FlowProblem] = []
    for f in flows:
        if (f.get("Status") or "") == "Obsolete" and (f.get("RunsInLastDay", 0) or 0) > 0:
            out.append(
                FlowProblem(
                    kind="obsolete_still_running",
                    flow_name=f.get("MasterLabel") or f.get("DeveloperName") or "<unknown>",
                    count=int(f.get("RunsInLastDay") or 0),
                )
            )
    return out


def _fetch_recent_interviews(soql_query=None) -> list[dict[str, Any]]:
    # FlowInterview is a data-API SObject — Tooling API rejects it with
    # "sObject type 'FlowInterview' is not supported."
    q = (
        "SELECT Id, InterviewLabel, InterviewStatus, CreatedDate "
        "FROM FlowInterview "
        f"WHERE CreatedDate > {_recent_cutoff_iso(FAILURE_WINDOW_HOURS)}"
    )
    sq = soql_query or salesforce_mcp.soql_query
    r = sq(q, 1000)
    return r.get("records", [])


def _fetch_flows(tooling_query=None) -> list[dict[str, Any]]:
    """Lightweight flow list — we augment with run-counts from interview rollup.

    Flow.DeveloperName lives on the parent FlowDefinition, so we traverse
    the `Definition` relationship and flatten into the flat shape the
    detectors expect.
    """
    q = (
        "SELECT Id, MasterLabel, Status, ProcessType, Definition.DeveloperName "
        "FROM Flow"
    )
    tq = tooling_query or salesforce_mcp.tooling_query
    r = tq(q, 1000)
    flows = r.get("records", [])
    for f in flows:
        dev = (f.get("Definition") or {}).get("DeveloperName") if isinstance(
            f.get("Definition"), dict
        ) else None
        if dev and "DeveloperName" not in f:
            f["DeveloperName"] = dev
    return flows


def poll(*, soql_query=None, tooling_query=None) -> list[dict[str, Any]]:
    """Run both checks, surface a task per distinct problem, return records."""
    interviews = _fetch_recent_interviews(soql_query=soql_query)
    flows = _fetch_flows(tooling_query=tooling_query)

    # Attach a crude run-count to each flow from the interview sample.
    by_name: dict[str, int] = {}
    for r in interviews:
        label = (r.get("InterviewLabel") or "").split("-")[0].strip()
        if label:
            by_name[label] = by_name.get(label, 0) + 1
    for f in flows:
        name = f.get("MasterLabel") or f.get("DeveloperName")
        if name:
            f["RunsInLastDay"] = by_name.get(name, 0)

    problems = detect_failures(interviews) + detect_obsolete_active(flows)

    surfaced: list[dict[str, Any]] = []
    for p in problems:
        source = f"revops_support:flow:{p.kind}:{p.flow_name}"
        title = (
            f"Flow {p.flow_name!r} has {p.count} error interview(s) in last 24h"
            if p.kind == "recent_failures"
            else f"Obsolete flow {p.flow_name!r} still firing ({p.count} runs/day)"
        )
        description = (
            f"Detected by revops_support flow_monitor. "
            f"kind={p.kind}, count={p.count}, sample_id={p.sample_id or 'n/a'}. "
            "Investigate via Tooling API FlowInterview / Flow records."
        )
        task_id = surface_task(
            source=source,
            title=title,
            description=description,
            category=TASK_CATEGORY,
            priority="high" if p.kind == "recent_failures" and p.count >= 5 else "medium",
            metadata={
                "kind": p.kind,
                "flow_name": p.flow_name,
                "count": p.count,
                "sample_id": p.sample_id,
            },
        )
        surfaced.append({"task_id": task_id, **p.__dict__})

    log.info("flow_monitor: surfaced %d problem(s)", len(surfaced))
    return surfaced
