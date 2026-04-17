"""Integration health poller — runs every 30 min.

Checks SF, Fireflies, Slack, Momentum, Vitally, Clay, Apollo, Nooks.
Writes integration_health rows ONLY on status change (no-op on stable).
Alerts O via DM on every change-to-unhealthy transition.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

INTEGRATIONS = [
    "salesforce", "fireflies", "slack", "momentum",
    "vitally", "clay", "apollo", "nooks",
]

# 30 min default — Vitally pushes events frequently during business hours so a
# >30min gap signals webhook-side breakage. Override in .env if a tenant has a
# legitimately quieter cadence.
_VITALLY_FRESHNESS_MAX_MIN = 30


def _record(integration: str, status: str, error: str | None = None) -> str | None:
    """Write row ONLY when status changed. Return previous status if it changed.

    First-ever check returns None (no prior state to compare). Same-status
    repeat polls skip the INSERT entirely to keep integration_health table
    small (was growing ~150 rows/hour at 8 integrations × every 30min).
    """
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
        if prev and prev[0] == status:
            return None
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
    return prev[0] if prev else None


def _key_present(env_key: str) -> bool:
    val = get_config(env_key)
    return bool(val) and val != "REPLACE"


def _classify_response(resp: httpx.Response) -> tuple[str, str | None]:
    """Map HTTP status → integration_health status."""
    if 200 <= resp.status_code < 300:
        return "healthy", None
    if resp.status_code in (401, 403):
        return "down", f"auth failed (HTTP {resp.status_code})"
    return "degraded", f"unexpected HTTP {resp.status_code}"


async def _check_salesforce() -> tuple[str, str | None]:
    try:
        from shared.mcp import salesforce_mcp
        r = salesforce_mcp.soql_query("SELECT COUNT(Id) c FROM User WHERE IsActive=true", limit=1)
        total = r.get("totalSize", 0) or len(r.get("records", []))
        return ("healthy", None) if total else ("degraded", "zero active users")
    except Exception as e:
        return "down", str(e)[:200]


async def _check_fireflies() -> tuple[str, str | None]:
    if not _key_present("FIREFLIES_API_KEY"):
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
    """Hit Vitally `/resources/accounts?limit=1` and verify webhook freshness.

    Vitally pushes account-level events to the configured webhook; if the most
    recent `lastInboundConnectionAt` across recently-touched accounts is >30
    min stale during business hours, the webhook chain is likely broken.
    """
    if not _key_present("VITALLY_API_KEY"):
        return "degraded", "no api key configured"
    base = (get_config("VITALLY_BASE_URL") or "https://rest.vitally.io").rstrip("/")
    url = f"{base}/resources/accounts?limit=5&sortBy=updatedAt&sortDirection=desc"
    try:
        with httpx.Client(timeout=8.0) as c:
            resp = c.get(
                url,
                headers={"Authorization": f"Basic {get_config('VITALLY_API_KEY')}"},
            )
    except Exception as e:
        return "down", str(e)[:200]
    status, err = _classify_response(resp)
    if status != "healthy":
        return status, err
    try:
        body = resp.json()
        accounts = body.get("results") or body.get("data") or []
        most_recent = None
        for acct in accounts:
            ts_raw = (
                acct.get("lastInboundConnectionAt")
                or acct.get("updatedAt")
                or acct.get("created_at")
            )
            if not ts_raw:
                continue
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if most_recent is None or ts > most_recent:
                most_recent = ts
        if most_recent is None:
            return "degraded", "no timestamps in Vitally accounts response"
        age_min = (datetime.now(timezone.utc) - most_recent).total_seconds() / 60
        if age_min > _VITALLY_FRESHNESS_MAX_MIN:
            return "degraded", f"webhook stale: most recent update {age_min:.0f}min ago"
        return "healthy", None
    except Exception as e:
        return "degraded", f"unable to parse Vitally response: {str(e)[:120]}"


async def _check_clay() -> tuple[str, str | None]:
    """Hit Clay `/v1/workflows` (cheap auth check; doesn't burn enrichment credits)."""
    if not _key_present("CLAY_API_KEY"):
        return "degraded", "no api key configured"
    base = (get_config("CLAY_BASE_URL") or "https://api.clay.com/v1").rstrip("/")
    try:
        with httpx.Client(timeout=8.0) as c:
            resp = c.get(
                f"{base}/workflows",
                headers={"Authorization": f"Bearer {get_config('CLAY_API_KEY')}"},
                params={"limit": 1},
            )
    except Exception as e:
        return "down", str(e)[:200]
    return _classify_response(resp)


async def _check_apollo() -> tuple[str, str | None]:
    """Hit Apollo `/v1/auth/health` — real auth-test endpoint, no credit cost."""
    if not _key_present("APOLLO_API_KEY"):
        return "degraded", "no api key configured"
    base = (get_config("APOLLO_BASE_URL") or "https://api.apollo.io/v1").rstrip("/")
    try:
        with httpx.Client(timeout=8.0) as c:
            resp = c.get(
                f"{base}/auth/health",
                headers={"X-Api-Key": get_config("APOLLO_API_KEY")},
            )
    except Exception as e:
        return "down", str(e)[:200]
    return _classify_response(resp)


async def _check_nooks() -> tuple[str, str | None]:
    """Hit Nooks `/api/v1/users/me` (typical auth-test pattern; cheap)."""
    if not _key_present("NOOKS_API_KEY"):
        return "degraded", "no api key configured"
    base = (get_config("NOOKS_BASE_URL") or "https://api.nooks.in/api/v1").rstrip("/")
    try:
        with httpx.Client(timeout=8.0) as c:
            resp = c.get(
                f"{base}/users/me",
                headers={"Authorization": f"Bearer {get_config('NOOKS_API_KEY')}"},
            )
    except Exception as e:
        return "down", str(e)[:200]
    return _classify_response(resp)


def _alert_o_dm(integration: str, prev_status: str, status: str, err: str | None) -> None:
    """Post a one-line DM to O on every change-to-unhealthy transition."""
    try:
        from shared.slack_dispatcher import SlackSender
        sender = SlackSender()
        o_dm = get_config("SLACK_TEST_CHANNEL") or "U08K2UTG3G8"
        msg = f":rotating_light: Integration `{integration}` transitioned `{prev_status}` → `{status}`"
        if err:
            msg += f"\n> {err}"
        sender.send(o_dm, msg)
    except Exception as e:
        log.exception("integration_health DM alert failed: %s", e)


async def poll() -> dict[str, Any]:
    checks = {
        "salesforce": _check_salesforce(),
        "fireflies": _check_fireflies(),
        "slack": _check_slack(),
        "momentum": _check_momentum(),
        "vitally": _check_vitally(),
        "clay": _check_clay(),
        "apollo": _check_apollo(),
        "nooks": _check_nooks(),
    }
    results: dict[str, Any] = {}
    for name, coro in checks.items():
        status, err = await coro
        changed = _record(name, status, err)
        results[name] = {"status": status, "error": err, "changed_from": changed}
        if changed and status != "healthy":
            log.warning("ALERT: %s transitioned %s -> %s (%s)", name, changed, status, err)
            _alert_o_dm(name, changed, status, err)
    return results


if __name__ == "__main__":
    print(asyncio.run(poll()))
