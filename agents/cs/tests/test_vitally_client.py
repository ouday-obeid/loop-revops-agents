"""M1 — Vitally REST client tests. httpx.MockTransport keeps all tests offline."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agents.cs.health.vitally_client import VitallyClient, VitallyError, classify_nps


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_auth_header_uses_api_key_basic():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"results": [], "next": None})

    async with VitallyClient(api_key="test-key", transport=_mock_transport(handler)) as c:
        await c.list_accounts()
    assert captured["auth"] and captured["auth"].startswith("Basic ")


@pytest.mark.asyncio
async def test_list_accounts_returns_json():
    body = {"results": [{"id": "v1", "externalId": "001SF1", "name": "Acme"}], "next": None}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    async with VitallyClient(api_key="k", transport=_mock_transport(handler)) as c:
        resp = await c.list_accounts(limit=50)
    assert resp == body


@pytest.mark.asyncio
async def test_iter_accounts_follows_cursor():
    pages = [
        {"results": [{"id": "v1"}, {"id": "v2"}], "next": "cursor-A"},
        {"results": [{"id": "v3"}], "next": None},
    ]
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = state["i"]
        state["i"] += 1
        return httpx.Response(200, json=pages[i])

    collected = []
    async with VitallyClient(api_key="k", transport=_mock_transport(handler)) as c:
        async for acct in c.iter_accounts(page_size=2):
            collected.append(acct["id"])

    assert collected == ["v1", "v2", "v3"]
    assert state["i"] == 2


@pytest.mark.asyncio
async def test_retry_on_429_succeeds():
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = state["i"]
        state["i"] += 1
        if i == 0:
            return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")
        return httpx.Response(200, json={"results": [], "next": None})

    async with VitallyClient(api_key="k", transport=_mock_transport(handler)) as c:
        resp = await c.list_accounts()
    assert resp == {"results": [], "next": None}
    assert state["i"] == 2  # retried exactly once


@pytest.mark.asyncio
async def test_500_retries_then_raises():
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["i"] += 1
        return httpx.Response(500, text="upstream down")

    with pytest.raises(VitallyError):
        async with VitallyClient(api_key="k", transport=_mock_transport(handler)) as c:
            await c.list_accounts()
    # MAX_RETRIES=3 → 1 initial + 3 retries = 4
    assert state["i"] == 4


@pytest.mark.asyncio
async def test_4xx_raises_immediately():
    state = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["i"] += 1
        return httpx.Response(403, text="forbidden")

    with pytest.raises(VitallyError):
        async with VitallyClient(api_key="k", transport=_mock_transport(handler)) as c:
            await c.list_accounts()
    assert state["i"] == 1  # no retry on 4xx


def test_classify_nps_boundaries():
    assert classify_nps(None) == "unknown"
    assert classify_nps(0) == "detractor"
    assert classify_nps(6) == "detractor"
    assert classify_nps(7) == "passive"
    assert classify_nps(8) == "passive"
    assert classify_nps(9) == "promoter"
    assert classify_nps(10) == "promoter"
