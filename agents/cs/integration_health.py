"""CS-specific integration health poller — runs every 30 min.

OO has a generic poller covering SF/Fireflies/Slack/Momentum/Vitally at the
reachability level. This CS-layer poller probes *CS-meaningful* conditions the
generic poller misses:

  - cs_vitally          — actual account list round-trip (not just key present)
  - cs_fireflies        — transcript list reachable (call intel feed for briefs)
  - cs_salesforce       — SOQL on Account reachable (renewal/churn writes depend on it)
  - cs_momentum_sync    — >=1 Momentum-sourced Task in last 7d (silent-break detector)
  - cs_nps_freshness    — ≥40% of cs_account_health rows have nps_at within 30d

On status *change* to degraded/down, creates an idempotent revops_support task
so the break is surfaced in the OO morning brief.

Agent isolation: writes to `integration_health` with `cs_*`-prefixed integration
names; the OO poller uses unprefixed names. No overlap.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

NPS_FRESHNESS_WINDOW_DAYS = 30
NPS_FRESHNESS_THRESHOLD = 0.40
MOMENTUM_WINDOW_DAYS = 7


def _record(integration: str, status: str, error: str | None = None) -> str | None:
    """Write health row. Return prior status when it changed, else None."""
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        prev = conn.execute(
            text(
                """SELECT status FROM integration_health WHERE integration = :i
                   ORDER BY checked_at DESC LIMIT 1"""
            ),
            {"i": integration},
        ).fetchone()
        conn.execute(
            text(
                """INSERT INTO integration_health
                       (integration, status, last_success, last_failure, error_message, checked_at)
                   VALUES (:i, :s, :ls, :lf, :e, :now)"""
            ),
            {
                "i": integration,
                "s": status,
                "ls": now if status == "healthy" else None,
                "lf": now if status != "healthy" else None,
                "e": error,
                "now": now,
            },
        )
    return prev[0] if prev and prev[0] != status else None


def _open_task(integration: str, status: str, error: str | None) -> None:
    """Create idempotent revops_support task when a CS integration degrades."""
    source = f"cs:integration_health:{integration}"
    title = f"CS integration '{integration}' is {status}"
    description = (
        f"The CS integration probe for `{integration}` transitioned to "
        f"`{status}`. Error: {error or 'n/a'}. "
        "Review logs at agents/cs/integration_health.py and the "
        "integration_health table."
    )
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s AND status != 'completed' LIMIT 1"),
            {"s": source},
        ).fetchone()
        if exists:
            return
        priority = "high" if status == "down" else "medium"
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('revops_support', :t, :d, 'pending', :p,
                           'cs_integration', :s, 'system', :m)"""
            ),
            {
                "t": title,
                "d": description,
                "p": priority,
                "s": source,
                "m": json.dumps({"integration": integration, "status": status, "error": error}),
            },
        )


async def _check_vitally(client_factory: Any | None = None) -> tuple[str, str | None]:
    key = get_config("VITALLY_API_KEY")
    if not key or key == "REPLACE":
        return "degraded", "VITALLY_API_KEY not configured"
    try:
        if client_factory is None:
            from agents.cs.health.vitally_client import VitallyClient
            client_factory = VitallyClient
        async with client_factory() as c:
            await c.list_accounts(limit=1)
        return "healthy", None
    except Exception as e:
        return "down", str(e)[:200]


async def _check_fireflies(fireflies_mcp: Any | None = None) -> tuple[str, str | None]:
    key = get_config("FIREFLIES_API_KEY")
    if not key or key == "REPLACE":
        return "degraded", "FIREFLIES_API_KEY not configured"
    try:
        if fireflies_mcp is None:
            from shared.mcp import fireflies_mcp as _ff
            fireflies_mcp = _ff
        fireflies_mcp.list_transcripts(limit=1)
        return "healthy", None
    except Exception as e:
        return "down", str(e)[:200]


async def _check_salesforce(sf_mcp: Any | None = None) -> tuple[str, str | None]:
    try:
        if sf_mcp is None:
            from shared.mcp import salesforce_mcp as _sf
            sf_mcp = _sf
        r = sf_mcp.soql_query("SELECT COUNT(Id) c FROM Account", limit=1)
        total = r.get("totalSize", 0) or len(r.get("records", []))
        return ("healthy", None) if total is not None else ("degraded", "no response shape")
    except Exception as e:
        return "down", str(e)[:200]


async def _check_momentum_sync(sf_mcp: Any | None = None) -> tuple[str, str | None]:
    """Count Momentum-sourced SF Tasks in last 7d. Zero = silent sync break."""
    try:
        if sf_mcp is None:
            from shared.mcp import salesforce_mcp as _sf
            sf_mcp = _sf
        cutoff = (datetime.now(timezone.utc) - timedelta(days=MOMENTUM_WINDOW_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        q = f"SELECT COUNT(Id) c FROM Task WHERE CreatedDate > {cutoff} AND Source__c = 'Momentum'"
        r = sf_mcp.soql_query(q, limit=1)
        total = r.get("totalSize", 0)
        if total == 0:
            records = r.get("records") or []
            total = records[0].get("c", 0) if records else 0
        if total == 0:
            return "down", f"zero Momentum-sourced Tasks in last {MOMENTUM_WINDOW_DAYS}d"
        return "healthy", None
    except Exception as e:
        return "degraded", f"check failed: {str(e)[:150]}"


def _check_nps_freshness(now: datetime | None = None) -> tuple[str, str | None]:
    """≥40% of tracked accounts must have NPS within 30d."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=NPS_FRESHNESS_WINDOW_DAYS)
    engine = get_engine()
    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM cs_account_health")).scalar() or 0
        fresh = (
            conn.execute(
                text("SELECT COUNT(*) FROM cs_account_health WHERE nps_at >= :c"),
                {"c": cutoff},
            ).scalar()
            or 0
        )
    if total == 0:
        return "degraded", "no cs_account_health rows yet"
    rate = fresh / total
    if rate >= NPS_FRESHNESS_THRESHOLD:
        return "healthy", None
    return "degraded", f"NPS freshness {rate:.1%} below {NPS_FRESHNESS_THRESHOLD:.0%} threshold"


async def poll(
    *,
    vitally_client_factory: Any | None = None,
    fireflies_mcp: Any | None = None,
    sf_mcp: Any | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run all CS probes. Returns per-integration {status, error, changed_from}."""
    vitally = await _check_vitally(vitally_client_factory)
    fireflies = await _check_fireflies(fireflies_mcp)
    salesforce = await _check_salesforce(sf_mcp)
    momentum = await _check_momentum_sync(sf_mcp)
    nps = _check_nps_freshness(now)

    probes = {
        "cs_vitally": vitally,
        "cs_fireflies": fireflies,
        "cs_salesforce": salesforce,
        "cs_momentum_sync": momentum,
        "cs_nps_freshness": nps,
    }
    results: dict[str, Any] = {}
    for name, (status, err) in probes.items():
        changed = _record(name, status, err)
        results[name] = {"status": status, "error": err, "changed_from": changed}
        if changed and status != "healthy":
            log.warning("CS ALERT: %s %s -> %s (%s)", name, changed, status, err)
            _open_task(name, status, err)
    return results


if __name__ == "__main__":  # pragma: no cover
    print(asyncio.run(poll()))
