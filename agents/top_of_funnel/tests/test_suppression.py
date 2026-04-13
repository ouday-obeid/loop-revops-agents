"""D3 DoD test for suppression — covers all 5 layers + cache TTL + fail-open.

The 10-email DoD matrix (see RUNBOOK.md):
  1. clean@example.com             → pass
  2. cached_supp@example.com       → layer 1 hit
  3. opt_out@example.com           → layer 2 (HasOptedOutOfEmail)
  4. dnc@example.com               → layer 2 (DoNotCall)
  5. customer@example.com          → layer 3 (Account.Type=Customer)
  6. recently_active@example.com   → layer 4 (LastActivityDate within 90d)
  7. stale_activity@example.com    → layer 4 miss (>90 days)
  8. competitor@olo.com            → layer 5
  9. expired_cache@example.com     → layer 1 miss (TTL expired), re-checks
 10. bad_email                      → input-validation suppression
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from agents.top_of_funnel import suppression
from agents.top_of_funnel.state import get_state_engine
from agents.top_of_funnel.suppression import (
    SuppressionResult,
    add_manual,
    clear_expired,
    is_suppressed,
)


# ---------- fake SF client ----------


def make_sf(*, leads=None, contacts=None, raises=False):
    """Return a function that emulates shared.mcp.salesforce_mcp.soql_query."""
    leads = leads or []
    contacts = contacts or []

    def fake(q: str):
        if raises:
            from shared.mcp.salesforce_mcp import SalesforceError
            raise SalesforceError("simulated SF outage")
        if "FROM Lead" in q:
            return {"records": list(leads)}
        if "FROM Contact" in q:
            return {"records": list(contacts)}
        return {"records": []}

    return fake


def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


@pytest.fixture(autouse=True)
def _reset_cache():
    """Every test starts with an empty suppression_cache."""
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM suppression_cache"))
    yield


# ------------------------------------------------------------- 10 DoD scenarios


@pytest.mark.asyncio
async def test_1_clean_email_not_suppressed():
    sf = make_sf(leads=[], contacts=[])
    r = await is_suppressed(
        "clean@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is False
    assert r.source == "none"


@pytest.mark.asyncio
async def test_2_cache_hit_short_circuits():
    """Manual add → cache → future call returns cached without hitting SF."""
    await add_manual("cached_supp@example.com", reason="SDR flagged")

    # SF would say "not suppressed", but cache should win.
    clean_sf = make_sf(leads=[], contacts=[])
    r = await is_suppressed(
        "cached_supp@example.com",
        sf_query=clean_sf,
        competitor_domains=set(),
    )
    assert r.suppressed is True
    assert r.source == "manual"
    assert "SDR flagged" in r.reason


@pytest.mark.asyncio
async def test_3_sf_opt_out_suppresses():
    sf = make_sf(
        contacts=[{"Id": "003x", "HasOptedOutOfEmail": True, "DoNotCall": False, "Account": {}}]
    )
    r = await is_suppressed(
        "opt_out@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is True
    assert r.source == "sf_dnc"
    assert "HasOptedOutOfEmail" in r.reason


@pytest.mark.asyncio
async def test_4_sf_dnc_suppresses():
    sf = make_sf(
        leads=[{"Id": "00Qx", "HasOptedOutOfEmail": False, "DoNotCall": True}]
    )
    r = await is_suppressed(
        "dnc@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is True
    assert r.source == "sf_dnc"
    assert "DoNotCall" in r.reason


@pytest.mark.asyncio
async def test_5_customer_account_suppresses():
    sf = make_sf(
        contacts=[{
            "Id": "003x",
            "HasOptedOutOfEmail": False,
            "DoNotCall": False,
            "Account": {"Type": "Customer", "LastActivityDate": None},
        }]
    )
    r = await is_suppressed(
        "customer@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is True
    assert r.source == "sf_customer"


@pytest.mark.asyncio
async def test_6_recent_activity_suppresses():
    sf = make_sf(
        contacts=[{
            "Id": "003x",
            "HasOptedOutOfEmail": False,
            "DoNotCall": False,
            "Account": {"Type": "Prospect", "LastActivityDate": _iso_days_ago(14)},
        }]
    )
    r = await is_suppressed(
        "recently_active@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is True
    assert r.source == "sf_recent_activity"


@pytest.mark.asyncio
async def test_7_stale_activity_not_suppressed():
    """LastActivityDate older than 90d → no layer-4 hit."""
    sf = make_sf(
        contacts=[{
            "Id": "003x",
            "HasOptedOutOfEmail": False,
            "DoNotCall": False,
            "Account": {"Type": "Prospect", "LastActivityDate": _iso_days_ago(200)},
        }]
    )
    r = await is_suppressed(
        "stale_activity@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is False
    assert r.source == "none"


@pytest.mark.asyncio
async def test_8_competitor_domain_suppresses():
    sf = make_sf(leads=[], contacts=[])
    r = await is_suppressed(
        "buyer@olo.com",
        sf_query=sf,
        competitor_domains={"olo.com", "flipdish.com"},
    )
    assert r.suppressed is True
    assert r.source == "competitor"
    assert "olo.com" in r.reason


@pytest.mark.asyncio
async def test_9_expired_cache_rechecks():
    """A cache row older than the TTL should NOT short-circuit; SF is re-queried."""
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO suppression_cache (email, suppressed, reason, source, checked_at)
                   VALUES ('expired_cache@example.com', 1, 'old reason', 'manual', :t)"""
            ),
            {"t": datetime.now(timezone.utc) - timedelta(days=30)},
        )

    # SF says clean. With a 7-day TTL, cache is expired → SF wins → NOT suppressed.
    sf = make_sf(leads=[], contacts=[])
    r = await is_suppressed(
        "expired_cache@example.com",
        sf_query=sf,
        competitor_domains=set(),
        cache_ttl_days=7,
    )
    assert r.suppressed is False
    assert r.source == "none"


@pytest.mark.asyncio
async def test_10_invalid_email_suppressed():
    r = await is_suppressed("not-an-email", sf_query=make_sf(), competitor_domains=set())
    assert r.suppressed is True
    assert r.source == "input"


# ------------------------------------------------------------------ fail-open


@pytest.mark.asyncio
async def test_sf_outage_fails_open_without_caching():
    sf = make_sf(raises=True)
    r = await is_suppressed(
        "outage@example.com",
        sf_query=sf,
        competitor_domains=set(),
    )
    assert r.suppressed is False
    assert r.source == "fail_open"

    # Make sure we did NOT cache the fail-open result (would mask recovery).
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT COUNT(*) FROM suppression_cache WHERE email='outage@example.com'")
        ).scalar()
    assert row == 0


# ------------------------------------------------------------------------- misc


@pytest.mark.asyncio
async def test_add_manual_persists():
    result = await add_manual("spam@example.com", reason="SDR complained 3x")
    assert "Suppressed" in result["text"]

    # Now is_suppressed should hit the cache with source='manual'.
    r = await is_suppressed("spam@example.com", sf_query=make_sf(), competitor_domains=set())
    assert r.suppressed is True
    assert r.source == "manual"


@pytest.mark.asyncio
async def test_add_manual_rejects_garbage():
    result = await add_manual("nope", reason="test")
    assert "email" in result["text"].lower()


def test_clear_expired_removes_old_rows_only():
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO suppression_cache (email, suppressed, reason, source, checked_at)
                   VALUES ('old@x.com', 1, '', 'manual', :old),
                          ('new@x.com', 1, '', 'manual', :new)"""
            ),
            {
                "old": datetime.now(timezone.utc) - timedelta(days=30),
                "new": datetime.now(timezone.utc) - timedelta(days=1),
            },
        )
    removed = clear_expired(ttl_days=7)
    assert removed == 1

    with engine.begin() as conn:
        remaining = conn.execute(text("SELECT email FROM suppression_cache")).fetchall()
    assert {r[0] for r in remaining} == {"new@x.com"}


def test_result_to_dict():
    r = SuppressionResult(True, "competitor:olo.com", "competitor")
    d = r.to_dict()
    assert d == {"suppressed": True, "reason": "competitor:olo.com", "source": "competitor"}


def test_competitor_domain_loader_uses_shared_first(tmp_path, monkeypatch):
    """If both shared/config/suppression_extras.yaml and local fallback exist,
    the shared path wins."""
    shared = tmp_path / "shared_extras.yaml"
    local = tmp_path / "local_extras.yaml"
    shared.write_text("competitors:\n  - domain: shared-wins.com\n")
    local.write_text("competitors:\n  - domain: local-loses.com\n")

    monkeypatch.setattr(suppression, "_SHARED_CONFIG", shared)
    monkeypatch.setattr(suppression, "_AGENT_CONFIG_FALLBACK", local)

    domains = suppression._load_competitor_domains()
    assert domains == {"shared-wins.com"}


def test_competitor_domain_loader_falls_back_to_local(tmp_path, monkeypatch):
    shared = tmp_path / "missing.yaml"
    local = tmp_path / "local.yaml"
    local.write_text("competitors:\n  - domain: fallback.com\n")

    monkeypatch.setattr(suppression, "_SHARED_CONFIG", shared)
    monkeypatch.setattr(suppression, "_AGENT_CONFIG_FALLBACK", local)

    domains = suppression._load_competitor_domains()
    assert domains == {"fallback.com"}
