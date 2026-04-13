"""Vitally REST client — async, retrying, Bearer-auth.

Docs: https://docs.vitally.io/rest-api/
Auth: HTTP Basic, API key as username, empty password.
Pagination: cursor via `?limit=N&from=<cursor>`.

Shape we rely on:
    account = {
        "id": "<vitally_uid>",
        "externalId": "<sf_account_id>",   # deterministic join key
        "name": "<display name>",
        "traits": {... arbitrary ...},
        "healthScore": {"current": <0-100>},
        "npsLatest": {"score": 0..10, "respondedAt": "<iso>"} | None,
        "lastSeenAt": "<iso>" | None,
    }

Tests inject an httpx.MockTransport so no network is touched.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

import httpx

from shared.secrets import require_secret

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://rest.vitally.io/resources/"
DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 3


class VitallyError(RuntimeError):
    pass


class VitallyClient:
    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key or require_secret("VITALLY_API_KEY")
        self._client = httpx.AsyncClient(
            base_url=base_url,
            auth=(self.api_key, ""),
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "VitallyClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def list_accounts(
        self, *, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["from"] = cursor
        return (await self._get("accounts", params=params)).json()

    async def iter_accounts(self, *, page_size: int = 100) -> AsyncIterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            page = await self.list_accounts(limit=page_size, cursor=cursor)
            for acct in page.get("results", []):
                yield acct
            cursor = page.get("next") or page.get("next_cursor")
            if not cursor:
                return

    async def get_account(self, account_id: str) -> dict[str, Any]:
        return (await self._get(f"accounts/{account_id}")).json()

    async def _get(self, path: str, *, params: dict | None = None) -> httpx.Response:
        backoff = 1.0
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = await self._client.get(path, params=params)
            except httpx.RequestError as e:
                last_exc = e
                if attempt == MAX_RETRIES:
                    raise VitallyError(f"GET {path} failed after retries: {e}") from e
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp) or backoff
                log.warning("vitally 429 on %s, sleeping %.1fs", path, retry_after)
                await asyncio.sleep(retry_after)
                backoff = min(backoff * 2, 30.0)
                continue
            if 500 <= resp.status_code < 600:
                if attempt == MAX_RETRIES:
                    raise VitallyError(f"GET {path} {resp.status_code}: {resp.text[:200]}")
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise VitallyError(f"GET {path} {resp.status_code}: {resp.text[:200]}")
            return resp

        raise VitallyError(f"GET {path} retries exhausted: {last_exc}")


def _parse_retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def classify_nps(score: int | None) -> str:
    if score is None:
        return "unknown"
    if score <= 6:
        return "detractor"
    if score <= 8:
        return "passive"
    return "promoter"
