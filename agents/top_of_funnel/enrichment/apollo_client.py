"""Apollo.io client — firmographic sourcing + decision-maker lookup.

Surface:
  search_accounts(filters)   — POST /accounts/search, returns list[dict]
  people_lookup(domain, titles=..., limit=...) — POST /people/search

Caching:
  Every call hashes its (endpoint, payload) to `query_hash` and looks up
  `apollo_query_cache` (SQLite). Hits < 1h old short-circuit the HTTP call.
  Rationale — re-running the pipeline in the same morning (e.g. Slack
  `@oo tof daily dry-run` after the 02:00 cron) shouldn't double-bill.

Soft-fail:
  auth errors / timeouts / 4xx / 5xx log `apollo_unavailable` and return
  an empty envelope. The pipeline then proceeds with whatever Clay+suppression
  can do alone — better to produce a small briefing than to freeze it.

HTTP wiring (POST to api.apollo.io) is a D4 stub — real calls ship in D5 once
the exact Apollo search-filter shape is confirmed against Loop's account.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from agents.top_of_funnel.state import get_state_engine

log = logging.getLogger(__name__)

APOLLO_BASE_URL = "https://api.apollo.io/v1"
_DEFAULT_CACHE_TTL_MIN = 60  # 1 hour


class ApolloUnavailable(Exception):
    """Soft-fail signal — callers catch + log; pipeline continues."""


@dataclass(frozen=True)
class ApolloAccount:
    domain: str
    name: str | None = None
    location_count: int | None = None
    estimated_num_employees: int | None = None
    industry: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "name": self.name,
            "location_count": self.location_count,
            "estimated_num_employees": self.estimated_num_employees,
            "industry": self.industry,
        }


@dataclass(frozen=True)
class ApolloPerson:
    email: str | None
    first_name: str | None
    last_name: str | None
    title: str | None
    linkedin_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "title": self.title,
            "linkedin_url": self.linkedin_url,
        }


# --------------------------------------------------------------------- caching


def _hash_payload(endpoint: str, payload: dict[str, Any]) -> str:
    blob = json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_lookup(query_hash: str, ttl_minutes: int) -> dict[str, Any] | None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT response_json FROM apollo_query_cache
                   WHERE query_hash = :h AND cached_at >= :cutoff"""
            ),
            {"h": query_hash, "cutoff": cutoff},
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        log.warning("apollo cache corrupt for %s: %s", query_hash, exc)
        return None


def _cache_write(query_hash: str, response: dict[str, Any]) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO apollo_query_cache (query_hash, response_json, cached_at)
                   VALUES (:h, :r, :n)
                   ON CONFLICT(query_hash) DO UPDATE SET
                     response_json = excluded.response_json,
                     cached_at = excluded.cached_at"""
            ),
            {
                "h": query_hash,
                "r": json.dumps(response),
                "n": datetime.now(timezone.utc),
            },
        )


def clear_expired(ttl_minutes: int = _DEFAULT_CACHE_TTL_MIN) -> int:
    """Drop cache rows older than ttl_minutes. Returns rows removed."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=ttl_minutes)
    engine = get_state_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM apollo_query_cache WHERE cached_at < :cutoff"),
            {"cutoff": cutoff},
        )
        return result.rowcount or 0


# ------------------------------------------------------------------ HTTP layer


async def _post(
    endpoint: str,
    payload: dict[str, Any],
    *,
    http_client: Any = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Real POST to Apollo. Raises ApolloUnavailable on any failure mode
    (network, 4xx, 5xx, JSON decode). The wrapper above soft-fails."""
    api_key = api_key or os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise ApolloUnavailable("APOLLO_API_KEY not configured")

    if http_client is None:
        # Lazy import: httpx is listed in Phase 0 deps but we'd rather
        # not load it at module import when tests inject a fake client.
        try:
            import httpx
        except ImportError as e:
            raise ApolloUnavailable(f"httpx unavailable: {e}") from e
        http_client = httpx.AsyncClient(timeout=30.0)
        close_after = True
    else:
        close_after = False

    url = f"{APOLLO_BASE_URL}{endpoint}"
    headers = {
        "Cache-Control": "no-cache",
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
    }
    try:
        resp = await http_client.post(url, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise ApolloUnavailable(
                f"apollo {endpoint} → HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return resp.json()
    except ApolloUnavailable:
        raise
    except Exception as exc:  # network/timeout/json-decode
        raise ApolloUnavailable(f"apollo {endpoint} failed: {exc}") from exc
    finally:
        if close_after:
            await http_client.aclose()


# ------------------------------------------------------------------- public API


async def search_accounts(
    *,
    filters: dict[str, Any],
    http_client: Any = None,
    cache_ttl_minutes: int = _DEFAULT_CACHE_TTL_MIN,
) -> list[ApolloAccount]:
    """Firmographic account search. Soft-fails to an empty list."""
    endpoint = "/accounts/search"
    query_hash = _hash_payload(endpoint, filters)

    cached = _cache_lookup(query_hash, cache_ttl_minutes)
    if cached is not None:
        return [_account_from_raw(r) for r in cached.get("accounts", [])]

    try:
        raw = await _post(endpoint, filters, http_client=http_client)
    except ApolloUnavailable as exc:
        log.warning("apollo_unavailable: %s", exc)
        return []

    accounts = raw.get("accounts") or raw.get("organizations") or []
    _cache_write(query_hash, {"accounts": accounts})
    return [_account_from_raw(a) for a in accounts]


async def people_lookup(
    *,
    domain: str,
    titles: list[str] | None = None,
    limit: int = 10,
    http_client: Any = None,
    cache_ttl_minutes: int = _DEFAULT_CACHE_TTL_MIN,
) -> list[ApolloPerson]:
    """Find decision-makers at `domain`. Soft-fails to an empty list."""
    payload: dict[str, Any] = {
        "q_organization_domains": domain,
        "page": 1,
        "per_page": limit,
    }
    if titles:
        payload["person_titles"] = list(titles)

    endpoint = "/mixed_people/search"
    query_hash = _hash_payload(endpoint, payload)

    cached = _cache_lookup(query_hash, cache_ttl_minutes)
    if cached is not None:
        return [_person_from_raw(r) for r in cached.get("people", [])]

    try:
        raw = await _post(endpoint, payload, http_client=http_client)
    except ApolloUnavailable as exc:
        log.warning("apollo_unavailable: %s", exc)
        return []

    people = raw.get("people") or raw.get("contacts") or []
    _cache_write(query_hash, {"people": people})
    return [_person_from_raw(p) for p in people]


# --------------------------------------------------------------- row → dataclass


def _account_from_raw(row: dict[str, Any]) -> ApolloAccount:
    return ApolloAccount(
        domain=(row.get("primary_domain") or row.get("domain") or "").lower(),
        name=row.get("name"),
        location_count=row.get("num_locations") or row.get("location_count"),
        estimated_num_employees=row.get("estimated_num_employees"),
        industry=row.get("industry"),
        raw=row,
    )


def _person_from_raw(row: dict[str, Any]) -> ApolloPerson:
    return ApolloPerson(
        email=row.get("email"),
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        title=row.get("title"),
        linkedin_url=row.get("linkedin_url"),
        raw=row,
    )
