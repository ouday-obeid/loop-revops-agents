"""Integration health poller — runs every 30 min.

Checks SF, Fireflies, Slack, Momentum, Vitally. Writes integration_health rows;
alerts O on status changes.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

INTEGRATIONS = ["salesforce", "fireflies", "slack", "momentum", "vitally"]


def _record(integration: str, status: str, error: str | None = None) -> str | None:
    """Write row. Return previous status if it changed, else None."""
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


async def _check_salesforce() -> tuple[str, str | None]:
    try:
        from shared.mcp import salesforce_mcp
        r = salesforce_mcp.soql_query("SELECT COUNT(Id) c FROM User WHERE IsActive=true", limit=1)
        total = r.get("totalSize", 0) or len(r.get("records", []))
        return ("healthy", None) if total else ("degraded", "zero active users")
    except Exception as e:
        return "down", str(e)[:200]


async def _check_fireflies() -> tuple[str, str | None]:
    if not get_config("FIREFLIES_API_KEY") or get_config("FIREFLIES_API_KEY") == "REPLACE":
        return "degraded", "no api key configured"
    try:
        from shared.mcp import fireflies_mcp
        rows = fireflies_mcp.list_transcripts(limit=1)
        return "healthy", None
    except Exception as e:
        return "down", str(e)[:200]


async def _check_slack(slack_client: Any | None = None) -> tuple[str, str | None]:
    if slack_client is None:
        try:
            from slack_sdk import WebClient
            from shared.secrets import get_secret
            tok = get_secret("SLACK_BOT_TOKEN")
            if not tok or "REPLACE" in tok:
                return "degraded", "no bot token configured"
            slack_client = WebClient(token=tok)
        except Exception as e:
            return "down", str(e)[:200]
    try:
        resp = slack_client.auth_test()
        return ("healthy", None) if resp.get("ok") else ("down", str(resp))
    except Exception as e:
        return "down", str(e)[:200]


async def _check_momentum() -> tuple[str, str | None]:
    """Detect 100%-hidden sync break: zero activity events logged in last 4h business hours."""
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5 or not (13 <= now.hour <= 23):  # rough EST business hours in UTC
        return "healthy", "outside business hours — skipped"
    try:
        from shared.mcp import salesforce_mcp
        cutoff = (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = f"SELECT COUNT(Id) c FROM Task WHERE CreatedDate > {cutoff} AND Source__c = 'Momentum'"
        r = salesforce_mcp.soql_query(q, limit=1)
        total = r.get("totalSize", 0) or len(r.get("records", []))
        if total == 0:
            return "down", "zero Momentum activity events in last 4h during business hours"
        return "healthy", None
    except Exception as e:
        return "degraded", f"check failed: {str(e)[:150]}"


async def _check_vitally() -> tuple[str, str | None]:
    if not get_config("VITALLY_API_KEY") or get_config("VITALLY_API_KEY") == "REPLACE":
        return "degraded", "not yet configured (Phase 1)"
    return "healthy", None


async def poll() -> dict[str, Any]:
    checks = {
        "salesforce": _check_salesforce(),
        "fireflies": _check_fireflies(),
        "slack": _check_slack(),
        "momentum": _check_momentum(),
        "vitally": _check_vitally(),
    }
    results: dict[str, Any] = {}
    for name, coro in checks.items():
        status, err = await coro
        changed = _record(name, status, err)
        results[name] = {"status": status, "error": err, "changed_from": changed}
        if changed and status != "healthy":
            log.warning("ALERT: %s transitioned %s -> %s (%s)", name, changed, status, err)
    return results


if __name__ == "__main__":
    print(asyncio.run(poll()))
