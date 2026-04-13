"""Routing — SDR territory assignment + round-robin rotation.

Segments (from `config/territory.yaml`):
  ENT (>=50 locations)  → Charles's team, round-robin
  MM  (10–49 locations) → Nate's team, round-robin
  SMB (<10 locations)   → Hutch/Henry queue

Assignment flow per lead:
  1. classify_segment(location_count) → 'ENT' | 'MM' | 'SMB'
  2. Read rotation list from territory.yaml for that segment.
  3. Filter to users whose email resolves to an ACTIVE SF user (via list_users()
     → cached 24h → `tof_sf_user_cache` table inside the agent DB).
  4. Pick the next slot via `tof_routing_state.last_assigned_index + 1 mod N`.
  5. If the entire team is inactive / unresolvable, fall back to
     `default_owner_id` (the tof-agent service user) + emit audit warning.

State:
  tof_routing_state — one row per segment, tracks `last_assigned_index`.
  Routing is deterministic given the same territory.yaml rotation order
  (yaml is checked-in; rotation changes are auditable via git blame).

Public surface:
  classify_segment(location_count) -> 'ENT' | 'MM' | 'SMB'
  assign_owner(lead, *, territory_cfg=None, list_users=..., now=None)
      -> RoutingResult(sdr_email, sdr_user_id, sdr_slack_id, segment, fallback)
  refresh_user_cache() -> int   (call at agent boot + daily)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from sqlalchemy import text

from agents.top_of_funnel.state import get_state_engine

log = logging.getLogger(__name__)

Segment = Literal["ENT", "MM", "SMB"]
_SEGMENTS: tuple[Segment, ...] = ("ENT", "MM", "SMB")

_AGENT_DIR = Path(__file__).parent
_TERRITORY_PATH = _AGENT_DIR / "config" / "territory.yaml"

_USER_CACHE_TTL_HOURS = 24


# ---------------------------------------------------------- user cache schema
# Introduced in D6. Added inline here (instead of state.sql) so the Phase 0
# amendment PR surface doesn't grow. Idempotent CREATE — safe to call repeatedly.
_USER_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS tof_sf_user_cache (
    email TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    cached_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _ensure_user_cache() -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text(_USER_CACHE_DDL))


# -------------------------------------------------------------------- result


@dataclass(frozen=True)
class RoutingResult:
    segment: Segment
    sdr_email: str
    sdr_user_id: str
    sdr_slack_id: str | None
    fallback: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment": self.segment,
            "sdr_email": self.sdr_email,
            "sdr_user_id": self.sdr_user_id,
            "sdr_slack_id": self.sdr_slack_id,
            "fallback": self.fallback,
            "reason": self.reason,
        }


# -------------------------------------------------------------------- config


def load_territory(path: Path | None = None) -> dict[str, Any]:
    path = path or _TERRITORY_PATH
    if not path.exists():
        raise FileNotFoundError(f"territory config missing: {path}")
    return yaml.safe_load(path.read_text()) or {}


def is_dept_head(email: str, territory_cfg: dict[str, Any] | None = None) -> bool:
    """True if `email` is listed under `dept_heads` in territory.yaml.

    Dept heads (Hutch VP Sales, Charles VP ENT) can invoke any `@oo tof`
    command without O-only gating. Currently consumed by downstream callers
    adding access control — the handler itself is open today.
    """
    if not email:
        return False
    cfg = territory_cfg or load_territory()
    heads = {e.lower() for e in (cfg.get("dept_heads") or [])}
    return email.lower() in heads


def classify_segment(location_count: int | None) -> Segment:
    """Map location count to segment.

    None / 0 / negative → SMB (conservative: route to the Hutch/Henry queue
    rather than escalate to ENT). Tested explicitly.
    """
    if location_count is None or location_count < 1:
        return "SMB"
    if location_count >= 50:
        return "ENT"
    if location_count >= 10:
        return "MM"
    return "SMB"


# ----------------------------------------------------------- user cache I/O


def refresh_user_cache(
    *,
    list_users_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> int:
    """Refresh `tof_sf_user_cache` from SF. Returns rows upserted.

    Injected `list_users_fn` is for tests. Production uses
    shared.mcp.salesforce_mcp.list_users.
    """
    _ensure_user_cache()
    if list_users_fn is None:
        from shared.mcp.salesforce_mcp import list_users as _lu
        list_users_fn = _lu

    try:
        users = list_users_fn(active_only=False) or []
    except Exception as exc:  # noqa: BLE001
        log.warning("routing: list_users failed, keeping stale cache: %s", exc)
        return 0

    engine = get_state_engine()
    now = datetime.now(timezone.utc)
    n = 0
    with engine.begin() as conn:
        for u in users:
            email = (u.get("Email") or "").lower().strip()
            if not email:
                continue
            conn.execute(
                text(
                    """INSERT INTO tof_sf_user_cache (email, user_id, name, is_active, cached_at)
                       VALUES (:e, :id, :nm, :a, :n)
                       ON CONFLICT(email) DO UPDATE SET
                         user_id=excluded.user_id,
                         name=excluded.name,
                         is_active=excluded.is_active,
                         cached_at=excluded.cached_at"""
                ),
                {
                    "e": email,
                    "id": u.get("Id"),
                    "nm": u.get("Name"),
                    "a": 1 if u.get("IsActive", True) else 0,
                    "n": now,
                },
            )
            n += 1
    return n


def _lookup_user(email: str, *, ttl_hours: int = _USER_CACHE_TTL_HOURS) -> dict[str, Any] | None:
    _ensure_user_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT user_id, name, is_active, cached_at
                   FROM tof_sf_user_cache
                   WHERE email = :e AND cached_at >= :cutoff"""
            ),
            {"e": email.lower().strip(), "cutoff": cutoff},
        ).fetchone()
    if row is None:
        return None
    return {"user_id": row[0], "name": row[1], "is_active": bool(row[2])}


# ------------------------------------------------------------- round-robin


def _next_index(segment: Segment, rotation_length: int) -> int:
    """Atomically increment per-segment rotation index, wrap at length."""
    if rotation_length <= 0:
        return 0
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT last_assigned_index FROM tof_routing_state WHERE segment = :s"),
            {"s": segment},
        ).fetchone()
        if row is None:
            idx = 0
            conn.execute(
                text(
                    """INSERT INTO tof_routing_state (segment, last_assigned_index, updated_at)
                       VALUES (:s, :i, :n)"""
                ),
                {"s": segment, "i": idx, "n": datetime.now(timezone.utc)},
            )
        else:
            idx = (int(row[0]) + 1) % rotation_length
            conn.execute(
                text(
                    """UPDATE tof_routing_state
                       SET last_assigned_index = :i, updated_at = :n
                       WHERE segment = :s"""
                ),
                {"i": idx, "n": datetime.now(timezone.utc), "s": segment},
            )
        return idx


# ---------------------------------------------------------------- assignment


def assign_owner(
    lead: dict[str, Any],
    *,
    territory_cfg: dict[str, Any] | None = None,
    auto_refresh_cache: bool = True,
    list_users_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> RoutingResult:
    """Pick the next SDR for this lead's segment.

    `lead` requires `location_count`. A missing count → SMB per classify_segment.
    `auto_refresh_cache=True` pulls SF users once per ttl — set False in tests.
    """
    cfg = territory_cfg or load_territory()
    segment = classify_segment(lead.get("location_count"))
    rotation = (cfg.get("segments", {}).get(segment) or {}).get("rotation") or []

    if auto_refresh_cache and not _cache_fresh():
        refresh_user_cache(list_users_fn=list_users_fn)

    # Build list of (email, slack_id, user_id) filtered to active SF users.
    slots: list[tuple[str, str | None, str]] = []
    for entry in rotation:
        email = (entry.get("email") or "").lower().strip()
        if not email:
            continue
        u = _lookup_user(email)
        if u and u["is_active"] and u["user_id"]:
            slots.append((email, entry.get("slack_id"), u["user_id"]))

    if not slots:
        return _fallback(cfg, segment, reason="no_active_rotation_members")

    idx = _next_index(segment, len(slots))
    email, slack_id, user_id = slots[idx]
    slack = slack_id if slack_id and slack_id != "PLACEHOLDER" else None
    return RoutingResult(
        segment=segment,
        sdr_email=email,
        sdr_user_id=user_id,
        sdr_slack_id=slack,
    )


def _cache_fresh(ttl_hours: int = _USER_CACHE_TTL_HOURS) -> bool:
    _ensure_user_cache()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT MAX(cached_at) FROM tof_sf_user_cache")
        ).fetchone()
    if not row or not row[0]:
        return False
    last = row[0]
    if isinstance(last, str):
        # SQLite returns ISO string; parse defensively.
        try:
            last = datetime.fromisoformat(last)
        except ValueError:
            return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last >= cutoff


def _fallback(cfg: dict[str, Any], segment: Segment, *, reason: str) -> RoutingResult:
    default_id = cfg.get("default_owner_id") or "PLACEHOLDER_TOF_AGENT_USER_ID"
    log.warning("routing: falling back for segment=%s reason=%s", segment, reason)
    return RoutingResult(
        segment=segment,
        sdr_email="tof-agent@tryloop.ai",
        sdr_user_id=default_id,
        sdr_slack_id=None,
        fallback=True,
        reason=reason,
    )
