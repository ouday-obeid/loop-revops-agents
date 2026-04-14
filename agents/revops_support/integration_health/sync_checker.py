"""Cross-system sync freshness probe.

For each tracked integration, the agent knows a SOQL-level signal that proves
data is *currently* flowing in from the upstream system. If that signal hasn't
moved in the expected window, the sync is considered stale and a task is
opened.

Signals (Phase 1):
  - vitally    : Account.Vitally_Last_Sync__c newer than 60 min
  - zenskar    : Account.Zenskar_Last_Sync__c newer than 24 h
  - docusign   : Contract.LastModifiedDate newer than 24 h (any envelope activity)
  - momentum   : Task.CreatedDate where Source__c='Momentum' newer than 24 h
  - nooks      : Task.CreatedDate where Source__c='Nooks' newer than 24 h
  - bq         : an external BQ freshness timestamp (probed via injected fn)

Each integration probe is an `(id, window_seconds, soql, evaluator)` tuple. The
evaluator receives the SOQL result and decides healthy/stale.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from shared.mcp import salesforce_mcp

from ._task_surface import surface_task

log = logging.getLogger(__name__)

TASK_CATEGORY = "sf_integration_health"


@dataclass
class Probe:
    integration: str
    max_staleness_seconds: int
    soql: str  # queries expected to return a single datetime-typed "freshness" field


def _parse_sf_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _extract_latest_timestamp(records: list[dict[str, Any]]) -> datetime | None:
    latest: datetime | None = None
    for r in records:
        for v in r.values():
            if not isinstance(v, str):
                continue
            dt = _parse_sf_iso(v)
            if dt and (latest is None or dt > latest):
                latest = dt
    return latest


PROBES: tuple[Probe, ...] = (
    Probe(
        integration="vitally",
        max_staleness_seconds=60 * 60,
        soql=(
            "SELECT MAX(Vitally_Last_Sync__c) latest FROM Account "
            "WHERE Vitally_Last_Sync__c != null"
        ),
    ),
    Probe(
        integration="zenskar",
        max_staleness_seconds=24 * 60 * 60,
        soql=(
            "SELECT MAX(Zenskar_Last_Sync__c) latest FROM Account "
            "WHERE Zenskar_Last_Sync__c != null"
        ),
    ),
    Probe(
        integration="docusign",
        max_staleness_seconds=24 * 60 * 60,
        soql="SELECT MAX(LastModifiedDate) latest FROM Contract",
    ),
    Probe(
        integration="momentum",
        max_staleness_seconds=24 * 60 * 60,
        soql=(
            "SELECT MAX(CreatedDate) latest FROM Task "
            "WHERE Source__c = 'Momentum'"
        ),
    ),
    Probe(
        integration="nooks",
        max_staleness_seconds=24 * 60 * 60,
        soql=(
            "SELECT MAX(CreatedDate) latest FROM Task WHERE Source__c = 'Nooks'"
        ),
    ),
)


def _check_probe(
    probe: Probe,
    *,
    soql_query: Callable[[str, int], dict[str, Any]],
    now: datetime,
) -> tuple[str, datetime | None]:
    """Return (status, latest_ts). status ∈ {'healthy','stale','error'}."""
    try:
        r = soql_query(probe.soql, 1)
    except Exception as e:  # noqa: BLE001
        log.warning("sync_checker %s query failed: %s", probe.integration, e)
        return "error", None
    latest = _extract_latest_timestamp(r.get("records") or [])
    if latest is None:
        return "stale", None
    age = (now - latest).total_seconds()
    return ("healthy" if age <= probe.max_staleness_seconds else "stale"), latest


def poll(*, soql_query=None, probes: tuple[Probe, ...] = PROBES) -> list[dict[str, Any]]:
    sq = soql_query or salesforce_mcp.soql_query
    now = datetime.now(timezone.utc)

    surfaced: list[dict[str, Any]] = []
    for p in probes:
        status, latest = _check_probe(p, soql_query=sq, now=now)
        if status == "healthy":
            continue
        latest_iso = latest.isoformat() if latest else "never"
        source = f"revops_support:sync:{p.integration}"
        title = f"{p.integration.title()} sync is {status} (latest signal: {latest_iso})"
        description = (
            f"Detected by revops_support sync_checker. "
            f"Expected data fresher than {p.max_staleness_seconds // 60} min. "
            f"Latest: {latest_iso}. Query: `{p.soql}`."
        )
        task_id = surface_task(
            source=source,
            title=title,
            description=description,
            category=TASK_CATEGORY,
            priority="high" if status == "error" else "medium",
            metadata={
                "integration": p.integration,
                "status": status,
                "latest": latest_iso,
                "max_staleness_seconds": p.max_staleness_seconds,
            },
        )
        surfaced.append({
            "task_id": task_id,
            "integration": p.integration,
            "status": status,
            "latest": latest_iso,
        })

    log.info("sync_checker: surfaced %d problem(s) across %d probe(s)",
             len(surfaced), len(probes))
    return surfaced
