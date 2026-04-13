"""Momentum API client — read-only.

Momentum is the SDR/AE call-tracking tool that auto-syncs outbound calls
into Salesforce as Tasks. When that sync breaks, calls made in Momentum
never appear in SF — invisible to managers and to the rep's own pipeline.
This client pulls recent calls so `momentum_sync_monitor.py` can diff
them against SF ActivityHistory.

Credentials via `require_secret("MOMENTUM_API_KEY")`. Base URL via
`get_config("MOMENTUM_BASE_URL")`, defaulting to the production tenant.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from shared.secrets import get_config, require_secret

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.momentum.io"


class MomentumError(RuntimeError):
    pass


def _base_url() -> str:
    return (get_config("MOMENTUM_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _client(timeout: float = 20.0) -> httpx.Client:
    return httpx.Client(
        base_url=_base_url(),
        headers={
            "Authorization": f"Bearer {require_secret('MOMENTUM_API_KEY')}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    with _client() as c:
        resp = c.get(path, params=params or {})
    if resp.status_code != 200:
        raise MomentumError(f"HTTP {resp.status_code} {path}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as e:
        raise MomentumError(f"non-JSON response from {path}") from e


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def list_recent_calls(hours: int = 4, limit: int = 500) -> list[dict[str, Any]]:
    """Fetch calls logged in Momentum in the last `hours`.

    Returns a list of normalized call dicts — see `_normalize_call` for the
    shape the sync monitor expects. Sync gaps beyond ~4h are typically
    where this detector earns its keep.
    """
    data = _get(
        "/v1/calls",
        params={"since": _since_iso(hours), "limit": limit},
    )
    raw = data.get("calls") or data.get("data") or []
    return [_normalize_call(c) for c in raw if c.get("id")]


def get_call(call_id: str) -> dict[str, Any]:
    data = _get(f"/v1/calls/{call_id}")
    raw = data.get("call") or data
    return _normalize_call(raw)


def _normalize_call(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten Momentum's call object into the shape the monitor relies on.

    Field names vary slightly across Momentum API versions; this keeps all
    the conditional .get() chains in one place instead of leaking through
    the detector.
    """
    rep = raw.get("rep") or raw.get("user") or {}
    contact = raw.get("contact") or raw.get("to") or {}
    sf_link = raw.get("salesforce") or raw.get("sf") or {}
    return {
        "id": raw["id"],
        "started_at": raw.get("started_at") or raw.get("start_time") or raw.get("timestamp"),
        "duration_seconds": int(raw.get("duration_seconds") or raw.get("duration") or 0),
        "direction": (raw.get("direction") or "").lower(),
        "disposition": raw.get("disposition") or raw.get("outcome"),
        "rep_email": (rep.get("email") or raw.get("rep_email") or "").lower() or None,
        "rep_name": rep.get("name") or raw.get("rep_name"),
        "contact_email": (contact.get("email") or raw.get("contact_email") or "").lower() or None,
        "contact_phone": contact.get("phone") or raw.get("contact_phone"),
        "sf_task_id": sf_link.get("task_id") or raw.get("salesforce_task_id"),
        "sf_synced": bool(sf_link.get("synced") if "synced" in sf_link else raw.get("sf_synced")),
    }


def _smoke() -> None:  # pragma: no cover — manual only
    calls = list_recent_calls(hours=1, limit=5)
    print(f"momentum calls fetched: {len(calls)}")


if __name__ == "__main__":  # pragma: no cover
    import sys
    if "--smoke" in sys.argv:
        _smoke()
