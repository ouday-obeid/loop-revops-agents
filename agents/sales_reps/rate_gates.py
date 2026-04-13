"""Local rate gates for sales_reps.

Separate from `shared.governance.RATE_LIMITS` — that dict is reserved for
cross-agent/platform buckets and edits require a shared/* change. Our
per-capability buckets live here and write to the shared `rate_limits` table
so they still show up in the Phase 0 audit surface.

Buckets (sales_reps only):
  - sales_reps_grader_hourly — 100/hr, guards Fireflies + Sonnet costs
  - sales_reps_coaching_dm_daily — 30/day, guards per-rep DM noise
  - sales_reps_sync_alert_hourly — 1/hr/integration, sync-break alert storm guard
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)

_LIMITS: dict[str, int] = {
    "sales_reps_grader_hourly": 100,
    "sales_reps_coaching_dm_daily": 30,
    "sales_reps_sync_alert_hourly": 1,
}


class RateGateExceeded(Exception):
    """Raised when a sales_reps local rate gate is exceeded."""


def _window_start(window_seconds: int) -> datetime:
    now = datetime.now(timezone.utc)
    if window_seconds >= 86400:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0)


def check(bucket: str, window_seconds: int = 3600, *, mode: Literal["hard", "soft"] = "hard") -> int:
    """Atomic increment; raise RateGateExceeded if over (hard) or log-warn (soft).

    Returns the post-increment count.
    """
    limit = _LIMITS.get(bucket)
    if limit is None:
        raise ValueError(f"Unknown sales_reps rate bucket: {bucket}")

    window = _window_start(window_seconds)
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, count FROM rate_limits WHERE bucket = :b AND window_start = :w"),
            {"b": bucket, "w": window},
        ).fetchone()
        if row:
            count = row[1] + 1
            if count > limit:
                if mode == "hard":
                    raise RateGateExceeded(f"{bucket}: {count}/{limit}")
                log.warning("rate_gate SOFT breach bucket=%s count=%s/%s", bucket, count, limit)
            conn.execute(
                text("UPDATE rate_limits SET count = :c WHERE id = :id"),
                {"c": count, "id": row[0]},
            )
            return count
        conn.execute(
            text(
                """INSERT INTO rate_limits (bucket, count, window_start, limit_value)
                   VALUES (:b, 1, :w, :l)"""
            ),
            {"b": bucket, "w": window, "l": limit},
        )
        return 1


def limit_for(bucket: str) -> int:
    return _LIMITS[bucket]


def list_buckets() -> tuple[str, ...]:
    return tuple(_LIMITS.keys())
