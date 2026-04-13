"""Describe cache — 24h TTL, SQLite-backed (describe_cache table from 0002 migration).

Describe calls are expensive (SF API round-trip, ~200-800ms each) and rarely
change between deploys. We cache the JSON per (org_alias, sobject) and serve
from cache when `fetched_at > now - 24h`. Misses hit the sf CLI and also
increment `revops_describe_calls_hourly` so we don't runaway on metadata ops.

Bust points:
  1. After a successful prod metadata deploy (caller invokes `bust(sobjects=...)`).
  2. Weekly cleanup of rows older than 7 days (caller runs `vacuum_stale()`).
  3. Manual: `python -m agents.revops_support.query.describe_cache --bust <sobject>`.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import check_rate_limit
from shared.mcp import salesforce_mcp
from shared.secrets import get_config

log = logging.getLogger(__name__)

_TTL = timedelta(hours=24)
_STALE_AFTER = timedelta(days=7)


def _active_alias(intent: str = "read") -> str:
    """Match the alias that salesforce_mcp._sf() would use for the given intent.

    Keeps cache keyed by the same alias the fetch hits, so switching between
    read and sandbox doesn't accidentally serve stale cross-org describes.
    """
    return salesforce_mcp._resolve_org_alias(intent)  # type: ignore[arg-type]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(v)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get(sobject: str, *, intent: str = "read", max_age: timedelta | None = None) -> dict[str, Any]:
    """Return cached describe if fresh, else fetch and cache.

    max_age overrides the default 24h TTL (for callers that need fresher data,
    e.g. right after a schema deploy).
    """
    alias = _active_alias(intent)
    ttl = max_age or _TTL
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT describe_json, fetched_at FROM describe_cache "
                "WHERE org_alias = :a AND sobject = :s"
            ),
            {"a": alias, "s": sobject},
        ).fetchone()

    if row:
        fetched_at = _parse_ts(row[1])
        if fetched_at and _now() - fetched_at < ttl:
            log.debug("describe_cache HIT alias=%s sobject=%s", alias, sobject)
            return json.loads(row[0])

    log.debug("describe_cache MISS alias=%s sobject=%s", alias, sobject)
    # Rate-limit metadata pressure: misses count, hits don't.
    check_rate_limit("revops_describe_calls_hourly", window_seconds=3600)
    describe = salesforce_mcp.describe_sobject(sobject)
    _upsert(alias, sobject, describe)
    return describe


def _upsert(alias: str, sobject: str, describe: dict[str, Any]) -> None:
    payload = json.dumps(describe)
    now = _now()
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id FROM describe_cache WHERE org_alias = :a AND sobject = :s"),
            {"a": alias, "s": sobject},
        ).fetchone()
        if existing:
            conn.execute(
                text(
                    "UPDATE describe_cache SET describe_json = :j, fetched_at = :t WHERE id = :id"
                ),
                {"j": payload, "t": now, "id": existing[0]},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO describe_cache (org_alias, sobject, describe_json, fetched_at) "
                    "VALUES (:a, :s, :j, :t)"
                ),
                {"a": alias, "s": sobject, "j": payload, "t": now},
            )


def bust(*, sobjects: list[str] | None = None, alias: str | None = None) -> int:
    """Delete cache rows. If sobjects omitted, wipes all for the alias.

    Returns number of rows deleted. Called after a successful prod deploy so
    the next describe hits SF fresh.
    """
    target_alias = alias or _active_alias("read")
    engine = get_engine()
    with engine.begin() as conn:
        if sobjects:
            placeholders = ", ".join(f":s{i}" for i in range(len(sobjects)))
            params: dict[str, Any] = {f"s{i}": s for i, s in enumerate(sobjects)}
            params["a"] = target_alias
            res = conn.execute(
                text(
                    f"DELETE FROM describe_cache WHERE org_alias = :a "
                    f"AND sobject IN ({placeholders})"
                ),
                params,
            )
        else:
            res = conn.execute(
                text("DELETE FROM describe_cache WHERE org_alias = :a"),
                {"a": target_alias},
            )
        return res.rowcount or 0


def vacuum_stale(older_than: timedelta = _STALE_AFTER) -> int:
    """Drop rows older than `older_than`. Default 7d (Sunday cron)."""
    cutoff = _now() - older_than
    engine = get_engine()
    with engine.begin() as conn:
        res = conn.execute(
            text("DELETE FROM describe_cache WHERE fetched_at < :c"),
            {"c": cutoff},
        )
        return res.rowcount or 0


def _main() -> None:
    parser = argparse.ArgumentParser(description="describe_cache maintenance")
    parser.add_argument("--bust", metavar="SOBJECT", help="bust cache for a single sobject")
    parser.add_argument("--bust-all", action="store_true", help="bust entire alias cache")
    parser.add_argument("--vacuum", action="store_true", help="delete rows older than 7 days")
    args = parser.parse_args()

    if args.bust:
        n = bust(sobjects=[args.bust])
        print(f"busted {n} row(s) for {args.bust}")
    elif args.bust_all:
        n = bust()
        print(f"busted {n} row(s) for alias {_active_alias('read')}")
    elif args.vacuum:
        n = vacuum_stale()
        print(f"vacuumed {n} stale row(s)")
    else:
        parser.print_help()


if __name__ == "__main__":
    _main()
