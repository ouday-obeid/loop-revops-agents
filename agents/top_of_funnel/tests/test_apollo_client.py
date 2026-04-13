"""D4 tests for Apollo client — cache hit/miss, soft-fail, payload shape."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel.enrichment import apollo_client
from agents.top_of_funnel.enrichment.apollo_client import (
    ApolloAccount,
    ApolloPerson,
    ApolloUnavailable,
    clear_expired,
    people_lookup,
    search_accounts,
)
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset_cache():
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM apollo_query_cache"))
    yield


# ------------------------------------------------------------ fake HTTP client


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None, text_body: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text_body or str(body or "")

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeHTTP:
    """Tracks posts; returns canned responses in order, or raises if script empty."""

    def __init__(self, responses: list[Any]):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]):
        self.calls.append((url, json, headers))
        if not self._responses:
            raise AssertionError(f"unexpected Apollo call to {url}")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def aclose(self):
        pass


# ------------------------------------------------------------ search_accounts


@pytest.mark.asyncio
async def test_search_accounts_cache_miss_then_hit():
    http = _FakeHTTP([
        _FakeResponse(
            200,
            {
                "accounts": [
                    {
                        "primary_domain": "franchisee.com",
                        "name": "Franchisee Co",
                        "num_locations": 47,
                        "estimated_num_employees": 450,
                        "industry": "Restaurants",
                    }
                ]
            },
        )
    ])

    result1 = await search_accounts(
        filters={"industry_keywords": ["restaurants"]},
        http_client=http,
    )
    assert len(result1) == 1
    assert isinstance(result1[0], ApolloAccount)
    assert result1[0].domain == "franchisee.com"
    assert result1[0].location_count == 47

    # Second call — identical filters — should hit cache; no new HTTP call.
    result2 = await search_accounts(
        filters={"industry_keywords": ["restaurants"]},
        http_client=http,
    )
    assert len(result2) == 1
    assert len(http.calls) == 1  # no second POST


@pytest.mark.asyncio
async def test_search_accounts_cache_different_filters_separate():
    http = _FakeHTTP([
        _FakeResponse(200, {"accounts": [{"primary_domain": "a.com"}]}),
        _FakeResponse(200, {"accounts": [{"primary_domain": "b.com"}]}),
    ])

    a = await search_accounts(filters={"industry": "restaurants"}, http_client=http)
    b = await search_accounts(filters={"industry": "hotels"}, http_client=http)

    assert {r.domain for r in a} == {"a.com"}
    assert {r.domain for r in b} == {"b.com"}
    assert len(http.calls) == 2


@pytest.mark.asyncio
async def test_search_accounts_expired_cache_refetches():
    # Seed stale row directly into cache.
    engine = get_state_engine()
    import hashlib
    import json

    endpoint = "/accounts/search"
    payload = {"industry": "restaurants"}
    h = hashlib.sha256(
        json.dumps({"endpoint": endpoint, "payload": payload}, sort_keys=True).encode()
    ).hexdigest()
    old = datetime.now(timezone.utc) - timedelta(hours=3)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO apollo_query_cache (query_hash, response_json, cached_at)
                   VALUES (:h, :r, :c)"""
            ),
            {"h": h, "r": json.dumps({"accounts": [{"primary_domain": "stale.com"}]}), "c": old},
        )

    # TTL = 60 min. Stale → refetch.
    http = _FakeHTTP([_FakeResponse(200, {"accounts": [{"primary_domain": "fresh.com"}]})])
    fresh = await search_accounts(
        filters=payload,
        http_client=http,
        cache_ttl_minutes=60,
    )
    assert [a.domain for a in fresh] == ["fresh.com"]
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_search_accounts_4xx_soft_fails():
    http = _FakeHTTP([_FakeResponse(401, {}, "unauthorized")])
    result = await search_accounts(filters={"x": 1}, http_client=http)
    assert result == []


@pytest.mark.asyncio
async def test_search_accounts_network_error_soft_fails():
    http = _FakeHTTP([TimeoutError("connect timed out")])
    result = await search_accounts(filters={"x": 1}, http_client=http)
    assert result == []


@pytest.mark.asyncio
async def test_search_accounts_handles_organizations_alias():
    """Some Apollo responses use `organizations` instead of `accounts`."""
    http = _FakeHTTP([
        _FakeResponse(200, {"organizations": [{"primary_domain": "alt.com", "name": "Alt"}]})
    ])
    result = await search_accounts(filters={"y": 2}, http_client=http)
    assert [a.domain for a in result] == ["alt.com"]


# --------------------------------------------------------------- people_lookup


@pytest.mark.asyncio
async def test_people_lookup_happy_path():
    http = _FakeHTTP([
        _FakeResponse(
            200,
            {
                "people": [
                    {
                        "email": "ops@franchisee.com",
                        "first_name": "Jane",
                        "last_name": "Doe",
                        "title": "Director of Ops",
                        "linkedin_url": "https://linkedin.com/in/jane",
                    }
                ]
            },
        )
    ])
    people = await people_lookup(
        domain="franchisee.com",
        titles=["Director of Operations"],
        limit=5,
        http_client=http,
    )
    assert len(people) == 1
    assert isinstance(people[0], ApolloPerson)
    assert people[0].email == "ops@franchisee.com"

    # Second call — cached.
    again = await people_lookup(
        domain="franchisee.com",
        titles=["Director of Operations"],
        limit=5,
        http_client=http,
    )
    assert again[0].email == "ops@franchisee.com"
    assert len(http.calls) == 1


@pytest.mark.asyncio
async def test_people_lookup_soft_fail():
    http = _FakeHTTP([_FakeResponse(500, {}, "server error")])
    people = await people_lookup(domain="x.com", http_client=http)
    assert people == []


@pytest.mark.asyncio
async def test_people_lookup_different_titles_cache_separately():
    http = _FakeHTTP([
        _FakeResponse(200, {"people": [{"email": "a@x.com"}]}),
        _FakeResponse(200, {"people": [{"email": "b@x.com"}]}),
    ])
    a = await people_lookup(domain="x.com", titles=["CEO"], http_client=http)
    b = await people_lookup(domain="x.com", titles=["COO"], http_client=http)
    assert a[0].email == "a@x.com"
    assert b[0].email == "b@x.com"
    assert len(http.calls) == 2


# -------------------------------------------------------------------- misc


def test_clear_expired_removes_only_old():
    engine = get_state_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO apollo_query_cache (query_hash, response_json, cached_at)
                   VALUES ('old', '{}', :old),
                          ('new', '{}', :new)"""
            ),
            {"old": now - timedelta(hours=3), "new": now - timedelta(minutes=10)},
        )
    removed = clear_expired(ttl_minutes=60)
    assert removed == 1

    with engine.begin() as conn:
        rows = conn.execute(text("SELECT query_hash FROM apollo_query_cache")).fetchall()
    assert {r[0] for r in rows} == {"new"}


def test_account_to_dict_shape():
    a = ApolloAccount(domain="x.com", name="X", location_count=3)
    d = a.to_dict()
    assert d["domain"] == "x.com"
    assert d["location_count"] == 3
    assert "raw" not in d  # raw intentionally excluded from the wire shape


def test_person_to_dict_shape():
    p = ApolloPerson(email="a@b.com", first_name="A", last_name="B", title="CEO")
    d = p.to_dict()
    assert d["email"] == "a@b.com"
    assert d["title"] == "CEO"


@pytest.mark.asyncio
async def test_missing_api_key_soft_fails(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    # http_client=None, no key → _post raises ApolloUnavailable → soft-fail empty.
    result = await search_accounts(filters={"z": 3}, http_client=None)
    assert result == []


def test_hash_stable_across_key_order():
    from agents.top_of_funnel.enrichment.apollo_client import _hash_payload

    h1 = _hash_payload("/x", {"a": 1, "b": 2})
    h2 = _hash_payload("/x", {"b": 2, "a": 1})
    assert h1 == h2  # sort_keys guarantees stability

    h3 = _hash_payload("/x", {"a": 1, "b": 3})
    assert h1 != h3  # different payload → different hash


# ------------------------------------------------ ApolloUnavailable propagation


@pytest.mark.asyncio
async def test_apollo_unavailable_not_raised_from_public_api():
    """Public surface is non-raising — soft-fails to [].
    ApolloUnavailable exists for internal tests that want to assert the
    HTTP layer raised, not for callers."""
    http = _FakeHTTP([_FakeResponse(401, {}, "nope")])
    # Must NOT raise ApolloUnavailable to caller.
    result = await search_accounts(filters={"x": 1}, http_client=http)
    assert result == []
