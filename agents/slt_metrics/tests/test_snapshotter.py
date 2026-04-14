"""Snapshotter — idempotent append, round-trip read, metadata integrity,
`latest_snapshot_date` across multi-day writes.

The session-autouse `_isolate_db` fixture (declared in `tests/conftest.py` and
picked up via `pyproject.toml::testpaths` including both `tests` and
`agents/slt_metrics/tests`) gives every test a fresh in-memory DB with the
slt_metrics migration applied.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import text

from agents.slt_metrics.pipeline import snapshotter
from agents.slt_metrics.types import (
    ContactRole,
    OppRecord,
    PillarScore,
    ScoredDeal,
)
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _clean_snapshots():
    """Session-scoped DB is shared across tests — truncate between cases so
    each test owns a clean pipeline_snapshots table."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pipeline_snapshots"))
    yield


def _opp(**overrides) -> OppRecord:
    base = dict(
        id="0061xABC001",
        name="Chick-fil-A MM",
        account_id="001ACC",
        account_name="CFA",
        account_website="cfa.com",
        account_type="Customer",
        owner_id="005OWN",
        owner_name="Nate",
        owner_role="MM",
        owner_manager="Hutch",
        stage="Proposal",
        is_closed=False,
        is_won=False,
        amount=120000.0,
        acv=90000.0,
        fixed_arr=90000.0,
        locations=42,
        type="New Business",
        lead_source="Inbound",
        close_date=date(2026, 6, 30),
        created_date=None,
        last_activity_date=date(2026, 4, 10),
        last_modified_date=None,
        last_stage_change_date=date(2026, 4, 5),
        days_since_stage_change=8,
        time_in_stage=12,
        probability_sf=75.0,
        description=None,
        next_steps=None,
        next_step_date=None,
        icp_score=0.87,
        segment="MM",
        products={"Balance": 1},
        contact_roles=[ContactRole("003CON", "Jane", "j@cfa.com", "VP", "EB", True)],
        raw={"Id": "0061xABC001", "StageName": "Proposal"},
    )
    base.update(overrides)
    return OppRecord(**base)


def _deal(opp: OppRecord, **overrides) -> ScoredDeal:
    base = dict(
        opp_id=opp.id,
        opp_name=opp.name,
        owner_name=opp.owner_name,
        account_name=opp.account_name,
        segment=opp.segment,
        stage=opp.stage,
        amount=opp.amount,
        acv=opp.acv,
        close_date=opp.close_date,
        score=72,
        probability=0.50,
        category="Commit",
        weighted_acv=45000.0,
        pillars={
            "icp": PillarScore(value=0.87, detail="sf-icp-score"),
            "stage": PillarScore(value=1.0, detail="Proposal"),
        },
        risk_flags=[],
        weights_version="v1-seed",
        raw=opp,
    )
    base.update(overrides)
    return ScoredDeal(**base)


# ------------------------------------------------------------------ write

def test_write_snapshot_inserts_every_deal():
    opps = [_opp(id=f"0061x{i:03d}") for i in range(3)]
    deals = [_deal(o) for o in opps]
    inserted = snapshotter.write_snapshot(deals, snapshot_date=date(2026, 4, 13))
    assert inserted == 3


def test_write_snapshot_empty_input_returns_zero():
    assert snapshotter.write_snapshot([], snapshot_date=date(2026, 4, 13)) == 0


def test_write_snapshot_idempotent_on_rerun():
    deal = _deal(_opp())
    first = snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    second = snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    assert first == 1
    assert second == 0  # ON CONFLICT DO NOTHING


def test_write_snapshot_same_opp_different_days_both_insert():
    opp = _opp()
    deal = _deal(opp)
    a = snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    b = snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 14))
    assert a == 1 and b == 1


# ------------------------------------------------------------------ read

def test_read_snapshot_returns_written_rows():
    deal = _deal(_opp())
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    rows = snapshotter.read_snapshot(date(2026, 4, 13))
    assert len(rows) == 1
    row = rows[0]
    assert row["opp_id"] == "0061xABC001"
    assert row["stage"] == "Proposal"
    assert row["score"] == 72
    assert row["category"] == "Commit"
    assert row["acv"] == 90000.0
    assert row["weighted_acv"] == 45000.0


def test_read_snapshot_metadata_roundtrip_pillars_and_raw():
    deal = _deal(_opp())
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    row = snapshotter.read_snapshot(date(2026, 4, 13))[0]
    meta = row["metadata"]
    assert meta["weights_version"] == "v1-seed"
    assert meta["pillars"]["icp"]["value"] == 0.87
    assert meta["pillars"]["icp"]["detail"] == "sf-icp-score"
    assert meta["risk_flags"] == []
    assert meta["sf_raw"]["Id"] == "0061xABC001"


def test_read_snapshot_orders_by_acv_descending():
    opps = [
        _opp(id="OP_SMALL", acv=10_000.0),
        _opp(id="OP_LARGE", acv=500_000.0),
        _opp(id="OP_MID",   acv=75_000.0),
    ]
    deals = [_deal(o, acv=o.acv, weighted_acv=o.acv * 0.5) for o in opps]
    snapshotter.write_snapshot(deals, snapshot_date=date(2026, 4, 13))
    rows = snapshotter.read_snapshot(date(2026, 4, 13))
    assert [r["opp_id"] for r in rows] == ["OP_LARGE", "OP_MID", "OP_SMALL"]


def test_read_snapshot_empty_day_returns_empty_list():
    assert snapshotter.read_snapshot(date(2026, 4, 13)) == []


def test_read_snapshot_survives_corrupt_metadata():
    # Write a row manually with non-JSON metadata to prove the deserializer
    # doesn't blow up the whole read.
    deal = _deal(_opp())
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_snapshots SET metadata = :m WHERE opp_id = :o"),
            {"m": "not-json{", "o": "0061xABC001"},
        )
    row = snapshotter.read_snapshot(date(2026, 4, 13))[0]
    assert row["metadata"] == {}


def test_read_snapshot_null_metadata_yields_empty_dict():
    deal = _deal(_opp())
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_snapshots SET metadata = NULL WHERE opp_id = :o"),
            {"o": "0061xABC001"},
        )
    row = snapshotter.read_snapshot(date(2026, 4, 13))[0]
    assert row["metadata"] == {}


# ------------------------------------------------------------------ latest_snapshot_date

def test_latest_snapshot_date_none_when_empty():
    assert snapshotter.latest_snapshot_date() is None


def test_latest_snapshot_date_returns_most_recent():
    deal = _deal(_opp())
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 10))
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 11))
    assert snapshotter.latest_snapshot_date() == date(2026, 4, 13)


def test_latest_snapshot_date_respects_before_cutoff():
    deal = _deal(_opp())
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 10))
    snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    # "yesterday" lookup
    assert snapshotter.latest_snapshot_date(before=date(2026, 4, 13)) == date(2026, 4, 10)
    # strictly-less-than — today equals today should return the prior entry
    assert snapshotter.latest_snapshot_date(before=date(2026, 4, 11)) == date(2026, 4, 10)


# ------------------------------------------------------------------ serialization

def test_scored_deal_without_raw_still_snapshots():
    deal = replace(_deal(_opp()), raw=None)
    inserted = snapshotter.write_snapshot([deal], snapshot_date=date(2026, 4, 13))
    assert inserted == 1
    row = snapshotter.read_snapshot(date(2026, 4, 13))[0]
    # owner_id/account_id come from ScoredDeal.raw.owner_id — without raw, they are null.
    assert row["owner_id"] is None
    assert row["account_id"] is None
    # metadata still carries pillar + weights info even without raw.
    assert row["metadata"]["weights_version"] == "v1-seed"
    assert "sf_raw" not in row["metadata"]
