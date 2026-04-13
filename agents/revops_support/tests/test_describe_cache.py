"""Describe cache tests — hit/miss, TTL, bust, vacuum."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from agents.revops_support.query import describe_cache
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _clear_cache_and_rate():
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM describe_cache"))
        conn.execute(
            text("DELETE FROM rate_limits WHERE bucket = 'revops_describe_calls_hourly'")
        )


@pytest.fixture
def fake_sf(monkeypatch):
    monkeypatch.setenv("SF_ORG_ALIAS", "salesops")
    monkeypatch.delenv("SF_WRITE_ORG_ALIAS", raising=False)
    with patch(
        "shared.mcp.salesforce_mcp.describe_sobject"
    ) as m:
        m.return_value = {"name": "Account", "fields": [{"name": "Id"}]}
        yield m


def test_miss_then_hit(fake_sf):
    r1 = describe_cache.get("Account")
    r2 = describe_cache.get("Account")
    assert r1 == r2
    # second call should be served from cache (sf called only once)
    assert fake_sf.call_count == 1


def test_expired_ttl_refetches(fake_sf):
    describe_cache.get("Account")
    # Force-expire by pushing fetched_at back 25h
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE describe_cache SET fetched_at = :t WHERE sobject = 'Account'"),
            {"t": stale},
        )
    describe_cache.get("Account")
    assert fake_sf.call_count == 2


def test_custom_max_age_tighter(fake_sf):
    describe_cache.get("Account")
    # 10min max_age — previous entry is fresh (just written) so still a hit.
    describe_cache.get("Account", max_age=timedelta(minutes=10))
    assert fake_sf.call_count == 1


def test_bust_single_sobject(fake_sf):
    describe_cache.get("Account")
    describe_cache.get("Opportunity")
    deleted = describe_cache.bust(sobjects=["Account"])
    assert deleted == 1
    # Account re-fetches, Opportunity still cached
    describe_cache.get("Account")
    describe_cache.get("Opportunity")
    assert fake_sf.call_count == 3  # 2 initial + 1 re-fetch


def test_bust_all_for_alias(fake_sf):
    describe_cache.get("Account")
    describe_cache.get("Opportunity")
    deleted = describe_cache.bust()
    assert deleted == 2


def test_vacuum_stale_drops_old_rows(fake_sf):
    describe_cache.get("Account")
    ancient = datetime.now(timezone.utc) - timedelta(days=30)
    with get_engine().begin() as conn:
        conn.execute(
            text("UPDATE describe_cache SET fetched_at = :t WHERE sobject = 'Account'"),
            {"t": ancient},
        )
    dropped = describe_cache.vacuum_stale()
    assert dropped == 1


def test_miss_increments_rate_limit(fake_sf):
    describe_cache.get("Account")
    with get_engine().begin() as conn:
        count = conn.execute(
            text(
                "SELECT count FROM rate_limits "
                "WHERE bucket = 'revops_describe_calls_hourly'"
            )
        ).scalar()
    assert count == 1
    describe_cache.get("Account")  # cache hit — no new rate-limit row
    with get_engine().begin() as conn:
        count = conn.execute(
            text(
                "SELECT count FROM rate_limits "
                "WHERE bucket = 'revops_describe_calls_hourly'"
            )
        ).scalar()
    assert count == 1
