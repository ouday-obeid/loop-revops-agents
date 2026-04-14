"""D6 tests for routing — segment classification, round-robin, inactive skip,
full-team-out fallback, cache TTL."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel import routing
from agents.top_of_funnel.routing import (
    RoutingResult,
    assign_owner,
    classify_segment,
    refresh_user_cache,
)
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset_routing_tables():
    routing._ensure_user_cache()
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tof_sf_user_cache"))
        conn.execute(text("DELETE FROM tof_routing_state"))
    yield


@pytest.fixture
def territory_cfg() -> dict[str, Any]:
    return {
        "default_owner_id": "005FALLBACK",
        "segments": {
            "ENT": {
                "min_locations": 50,
                "rotation": [
                    {"email": "taylor@x.com", "slack_id": "U_TAYLOR"},
                    {"email": "clay@x.com", "slack_id": "U_CLAY"},
                    {"email": "daniel@x.com", "slack_id": "U_DAN"},
                ],
            },
            "MM": {
                "min_locations": 10,
                "max_locations": 49,
                "rotation": [
                    {"email": "carlton@x.com", "slack_id": "U_CARL"},
                    {"email": "eric@x.com", "slack_id": "U_ERIC"},
                ],
            },
            "SMB": {
                "max_locations": 9,
                "rotation": [
                    {"email": "hutch@x.com", "slack_id": "U_HUTCH"},
                    {"email": "henry@x.com", "slack_id": "U_HENRY"},
                ],
            },
        },
    }


def _seed_cache(users: list[dict[str, Any]]):
    """Directly inject a fresh SF user cache (bypasses refresh_user_cache)."""
    engine = get_state_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for u in users:
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
                    "e": u["email"].lower(),
                    "id": u["user_id"],
                    "nm": u.get("name"),
                    "a": 1 if u.get("is_active", True) else 0,
                    "n": now,
                },
            )


# ----------------------------------------------------------- classify_segment


@pytest.mark.parametrize(
    "count,expected",
    [
        (None, "SMB"),
        (0, "SMB"),
        (1, "SMB"),
        (9, "SMB"),
        (10, "MM"),
        (49, "MM"),
        (50, "ENT"),
        (500, "ENT"),
    ],
)
def test_classify_segment_bands(count, expected):
    assert classify_segment(count) == expected


def test_classify_segment_negative_defaults_smb():
    assert classify_segment(-1) == "SMB"


# ------------------------------------------------------------------- basic assign


def test_assign_ent_goes_to_first_active_member(territory_cfg):
    _seed_cache([
        {"email": "taylor@x.com", "user_id": "005TAY"},
        {"email": "clay@x.com", "user_id": "005CLA"},
        {"email": "daniel@x.com", "user_id": "005DAN"},
    ])
    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert isinstance(r, RoutingResult)
    assert r.segment == "ENT"
    assert r.sdr_email == "taylor@x.com"
    assert r.sdr_user_id == "005TAY"
    assert r.sdr_slack_id == "U_TAYLOR"
    assert r.fallback is False


def test_assign_mm_routes_to_mm_rotation(territory_cfg):
    _seed_cache([
        {"email": "carlton@x.com", "user_id": "005CAR"},
        {"email": "eric@x.com", "user_id": "005ERI"},
    ])
    r = assign_owner(
        {"location_count": 25},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert r.segment == "MM"
    assert r.sdr_email == "carlton@x.com"


def test_assign_smb_routes_to_smb_rotation(territory_cfg):
    _seed_cache([
        {"email": "hutch@x.com", "user_id": "005HUT"},
        {"email": "henry@x.com", "user_id": "005HEN"},
    ])
    r = assign_owner(
        {"location_count": 4},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert r.segment == "SMB"
    assert r.sdr_email == "hutch@x.com"


def test_assign_missing_location_defaults_smb(territory_cfg):
    _seed_cache([
        {"email": "hutch@x.com", "user_id": "005HUT"},
    ])
    r = assign_owner({}, territory_cfg=territory_cfg, auto_refresh_cache=False)
    assert r.segment == "SMB"


# ----------------------------------------------------------- round-robin


def test_round_robin_cycles_through_rotation(territory_cfg):
    _seed_cache([
        {"email": "taylor@x.com", "user_id": "005TAY"},
        {"email": "clay@x.com", "user_id": "005CLA"},
        {"email": "daniel@x.com", "user_id": "005DAN"},
    ])
    emails = []
    for _ in range(7):
        r = assign_owner(
            {"location_count": 120},
            territory_cfg=territory_cfg,
            auto_refresh_cache=False,
        )
        emails.append(r.sdr_email)

    # With 3 members, 7 assignments should land: tay, clay, dan, tay, clay, dan, tay
    assert emails == [
        "taylor@x.com", "clay@x.com", "daniel@x.com",
        "taylor@x.com", "clay@x.com", "daniel@x.com",
        "taylor@x.com",
    ]


def test_segments_rotate_independently(territory_cfg):
    _seed_cache([
        {"email": "taylor@x.com", "user_id": "005TAY"},
        {"email": "clay@x.com", "user_id": "005CLA"},
        {"email": "daniel@x.com", "user_id": "005DAN"},
        {"email": "carlton@x.com", "user_id": "005CAR"},
        {"email": "eric@x.com", "user_id": "005ERI"},
    ])
    # ENT one, MM one — each should get the FIRST of their rotation.
    ent = assign_owner(
        {"location_count": 100}, territory_cfg=territory_cfg, auto_refresh_cache=False
    )
    mm = assign_owner(
        {"location_count": 20}, territory_cfg=territory_cfg, auto_refresh_cache=False
    )
    assert ent.sdr_email == "taylor@x.com"
    assert mm.sdr_email == "carlton@x.com"


# ------------------------------------------------------------ inactive skip


def test_inactive_user_skipped(territory_cfg):
    _seed_cache([
        {"email": "taylor@x.com", "user_id": "005TAY", "is_active": False},
        {"email": "clay@x.com", "user_id": "005CLA", "is_active": True},
        {"email": "daniel@x.com", "user_id": "005DAN", "is_active": True},
    ])
    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    # Taylor filtered out — first active is Clay.
    assert r.sdr_email == "clay@x.com"


def test_unresolved_email_skipped(territory_cfg):
    """Only Clay is in the SF user cache — the other rotation emails skipped."""
    _seed_cache([
        {"email": "clay@x.com", "user_id": "005CLA"},
    ])
    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert r.sdr_email == "clay@x.com"


# ----------------------------------------------------------- full-team-out


def test_all_inactive_returns_fallback(territory_cfg):
    _seed_cache([
        {"email": "taylor@x.com", "user_id": "005TAY", "is_active": False},
        {"email": "clay@x.com", "user_id": "005CLA", "is_active": False},
        {"email": "daniel@x.com", "user_id": "005DAN", "is_active": False},
    ])
    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert r.fallback is True
    assert r.sdr_user_id == "005FALLBACK"
    assert "no_active" in r.reason


def test_empty_rotation_returns_fallback(territory_cfg):
    """Rotation list blank → fallback, don't crash."""
    territory_cfg["segments"]["ENT"]["rotation"] = []
    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert r.fallback is True


# ----------------------------------------------------------------- cache I/O


def test_refresh_user_cache_upserts():
    fake_users = [
        {"Id": "005AAA", "Name": "Alice", "Email": "alice@x.com", "IsActive": True},
        {"Id": "005BBB", "Name": "Bob", "Email": "bob@x.com", "IsActive": False},
    ]

    def fake_list(**kw):
        return fake_users

    n = refresh_user_cache(list_users_fn=fake_list)
    assert n == 2

    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT email, user_id, is_active FROM tof_sf_user_cache")).fetchall()
    assert {r[0] for r in rows} == {"alice@x.com", "bob@x.com"}
    by_email = {r[0]: r for r in rows}
    assert by_email["alice@x.com"][2] == 1
    assert by_email["bob@x.com"][2] == 0


def test_refresh_user_cache_sf_failure_preserves_stale():
    _seed_cache([
        {"email": "keep@x.com", "user_id": "005KEEP"},
    ])

    def boom(**kw):
        raise RuntimeError("sf outage")

    n = refresh_user_cache(list_users_fn=boom)
    assert n == 0

    # Stale entry intact.
    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT email FROM tof_sf_user_cache")).fetchall()
    assert {r[0] for r in rows} == {"keep@x.com"}


def test_refresh_ignores_users_without_email():
    def fake_list(**kw):
        return [
            {"Id": "005AAA", "Name": "Alice", "Email": "alice@x.com", "IsActive": True},
            {"Id": "005BBB", "Name": "Bot", "Email": None, "IsActive": True},
        ]

    n = refresh_user_cache(list_users_fn=fake_list)
    assert n == 1  # bot with no email skipped


def test_cache_expired_triggers_refresh(territory_cfg):
    """A stale cached user (cached > TTL ago) is NOT used; if auto_refresh is on,
    assign_owner pulls fresh users via the injected list_users_fn."""
    # Seed a stale row (36h old).
    engine = get_state_engine()
    old = datetime.now(timezone.utc) - timedelta(hours=36)
    routing._ensure_user_cache()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_sf_user_cache (email, user_id, name, is_active, cached_at)
                   VALUES ('taylor@x.com', '005STALE', 'Old', 1, :t)"""
            ),
            {"t": old},
        )

    # list_users returns a fresh row with a different user_id.
    def fake_list(**kw):
        return [{"Id": "005FRESH", "Name": "T", "Email": "taylor@x.com", "IsActive": True}]

    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=True,
        list_users_fn=fake_list,
    )
    assert r.sdr_user_id == "005FRESH"
    assert r.fallback is False


def test_placeholder_slack_id_becomes_none(territory_cfg):
    """Territory entries with slack_id='PLACEHOLDER' should not leak that
    sentinel into routing output — slack_id must be None so the briefer
    falls back to email-based lookup."""
    territory_cfg["segments"]["ENT"]["rotation"][0]["slack_id"] = "PLACEHOLDER"
    _seed_cache([{"email": "taylor@x.com", "user_id": "005TAY"}])
    r = assign_owner(
        {"location_count": 120},
        territory_cfg=territory_cfg,
        auto_refresh_cache=False,
    )
    assert r.sdr_slack_id is None


def test_routing_result_to_dict():
    r = RoutingResult(
        segment="ENT",
        sdr_email="a@b.com",
        sdr_user_id="005A",
        sdr_slack_id="UABC",
    )
    d = r.to_dict()
    assert d["segment"] == "ENT"
    assert d["fallback"] is False
