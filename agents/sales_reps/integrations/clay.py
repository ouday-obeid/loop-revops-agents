"""Clay enrichment client — decision-maker lookup + contact enrichment.

Phase 1 uses Clay's HTTP API for two jobs:
  1. `find_decision_makers(domain, titles)` — find buying-committee contacts
     by company domain and role title, returns name+title+LinkedIn+email.
  2. `enrich_contact(email)` — hit Clay's person endpoint to get the
     contact's LinkedIn URL, title, seniority for a known email.

Cost posture: Clay bills per lookup, so enrichment is Grade A/B only per
CLAUDE.md. The pre-demo brief caps at 5 decision-makers per company.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from shared.secrets import get_config, require_secret

log = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.clay.com/v1"

# Target seniority/role tokens worth enriching on a pre-demo brief.
DEFAULT_TITLES: tuple[str, ...] = (
    "CEO", "Founder", "COO", "CFO",
    "VP Operations", "VP Finance", "Director of Operations",
    "Head of Operations", "Head of Finance",
)


class ClayError(RuntimeError):
    pass


def _base_url() -> str:
    return (get_config("CLAY_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def _client(timeout: float = 25.0) -> httpx.Client:
    return httpx.Client(
        base_url=_base_url(),
        headers={
            "Authorization": f"Bearer {require_secret('CLAY_API_KEY')}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    with _client() as c:
        resp = c.post(path, json=body)
    if resp.status_code >= 400:
        raise ClayError(f"HTTP {resp.status_code} {path}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as e:
        raise ClayError(f"non-JSON from {path}") from e


def find_decision_makers(
    domain: str,
    *,
    titles: tuple[str, ...] | list[str] = DEFAULT_TITLES,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Look up decision-makers at a company by domain + target titles."""
    if not domain:
        return []
    data = _post(
        "/people/search",
        {"company_domain": domain, "titles": list(titles), "limit": limit},
    )
    raw = data.get("people") or data.get("results") or []
    return [_normalize_person(p) for p in raw if p]


def enrich_contact(email: str) -> dict[str, Any] | None:
    """Single-contact enrichment keyed by email."""
    if not email:
        return None
    try:
        data = _post("/people/enrich", {"email": email})
    except ClayError as e:
        log.warning("clay enrich_contact failed for %s: %s", email, e)
        return None
    raw = data.get("person") or data
    if not raw or not raw.get("email"):
        return None
    return _normalize_person(raw)


def _normalize_person(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": raw.get("name") or raw.get("full_name"),
        "email": (raw.get("email") or "").lower() or None,
        "title": raw.get("title") or raw.get("job_title"),
        "seniority": raw.get("seniority"),
        "linkedin_url": raw.get("linkedin_url") or raw.get("linkedin"),
        "company": (raw.get("company") or {}).get("name") if isinstance(raw.get("company"), dict)
                   else raw.get("company"),
        "source": "clay",
    }
