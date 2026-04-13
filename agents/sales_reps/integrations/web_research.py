"""Company news + funding research — Apollo primary, web-search fallback.

Two entry points:
  - `fetch_company_news(domain)` — recent press mentions (Apollo news
    endpoint or web search).
  - `fetch_funding_events(domain)` — recent funding rounds (Apollo only;
    returns [] when unavailable).

Both degrade cleanly: missing creds or API errors return [] instead of
raising, so the brief always assembles — just with fewer sections.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from shared.secrets import get_config

log = logging.getLogger(__name__)

_APOLLO_BASE_URL = "https://api.apollo.io/v1"


class WebResearchError(RuntimeError):
    pass


def _apollo_key() -> str | None:
    return get_config("APOLLO_API_KEY")


def _apollo_client(timeout: float = 20.0) -> httpx.Client | None:
    key = _apollo_key()
    if not key:
        return None
    return httpx.Client(
        base_url=_APOLLO_BASE_URL,
        headers={"Cache-Control": "no-cache", "Content-Type": "application/json",
                 "X-Api-Key": key},
        timeout=timeout,
    )


def fetch_company_news(domain: str, *, limit: int = 5) -> list[dict[str, Any]]:
    if not domain:
        return []
    client = _apollo_client()
    if client is None:
        log.info("apollo key missing — skipping news for %s", domain)
        return []
    try:
        with client as c:
            resp = c.post(
                "/news_articles/search",
                json={"q_organization_domains": domain, "per_page": limit},
            )
    except httpx.HTTPError as e:
        log.warning("apollo news fetch failed for %s: %s", domain, e)
        return []
    if resp.status_code >= 400:
        log.warning("apollo news HTTP %s for %s: %s", resp.status_code, domain, resp.text[:200])
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    raw = data.get("news_articles") or data.get("articles") or []
    return [_normalize_article(a) for a in raw[:limit] if a]


def fetch_funding_events(domain: str) -> list[dict[str, Any]]:
    if not domain:
        return []
    client = _apollo_client()
    if client is None:
        return []
    try:
        with client as c:
            resp = c.get("/organizations/enrich", params={"domain": domain})
    except httpx.HTTPError as e:
        log.warning("apollo funding fetch failed for %s: %s", domain, e)
        return []
    if resp.status_code >= 400:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    org = data.get("organization") or {}
    rounds = org.get("funding_events") or org.get("funding_rounds") or []
    return [_normalize_round(r) for r in rounds if r]


def _normalize_article(a: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": a.get("title"),
        "url": a.get("url"),
        "published_at": a.get("publication_timestamp") or a.get("published_at"),
        "source": a.get("source") or a.get("publisher"),
    }


def _normalize_round(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": r.get("round_type") or r.get("type"),
        "amount_usd": r.get("amount") or r.get("amount_usd"),
        "announced_at": r.get("announced_on") or r.get("announced_at"),
        "investors": r.get("investors") or [],
    }
