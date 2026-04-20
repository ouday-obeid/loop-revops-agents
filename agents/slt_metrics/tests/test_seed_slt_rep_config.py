"""Integration tests for scripts/seed_slt_rep_config.py.

Uses the session-scoped isolated DB from conftest.py (migrations 0004 + 0005
already applied). Verifies seeded row counts, idempotency, and that
metadata/manager attribution is correctly recorded.
"""
from __future__ import annotations

import importlib
import json

import pytest
from sqlalchemy import text

from agents.slt_metrics.pipeline.planning import AE_ROSTER, SDR_ROSTER
from shared.db.connection import get_engine


@pytest.fixture
def clean_rep_config():
    """Delete + restore rows inside a single test to avoid cross-test bleed."""
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(text("SELECT owner_name FROM rep_config")).fetchall()
        conn.execute(text("DELETE FROM rep_config"))
    yield
    # Restore is unnecessary — subsequent tests re-seed as needed.


def _seed_main():
    """Import-then-call so the seed script uses the fixture's DB engine."""
    mod = importlib.import_module("scripts.seed_slt_rep_config")
    return mod.main()


def test_seed_inserts_full_roster(clean_rep_config):
    assert _seed_main() == 0
    engine = get_engine()
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM rep_config")).scalar()
    assert count == len(AE_ROSTER) + len(SDR_ROSTER)


def test_seed_is_idempotent(clean_rep_config):
    _seed_main()
    _seed_main()  # Second run should not duplicate rows (owner_name PK).
    engine = get_engine()
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM rep_config")).scalar()
    assert count == len(AE_ROSTER) + len(SDR_ROSTER)


def test_seed_assigns_managers_via_metadata(clean_rep_config):
    _seed_main()
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT owner_name, metadata FROM rep_config WHERE owner_name IN "
            "('Alexis Marrero', 'Simon Salomon', 'Alex Reyes', 'Nick Barbo')"
        )).fetchall()

    meta = {row[0]: json.loads(row[1]) for row in rows}
    assert meta["Alexis Marrero"]["manager"] == "Hutch"
    assert meta["Simon Salomon"]["manager"] == "Nate"
    assert meta["Alex Reyes"]["manager"] == "IC"
    assert meta["Nick Barbo"]["manager"] == "Nate"


def test_seed_populates_quota_for_aes_only(clean_rep_config):
    _seed_main()
    engine = get_engine()
    with engine.connect() as conn:
        ae_quotas = conn.execute(text(
            "SELECT owner_name, annual_quota, quarterly_quota "
            "FROM rep_config WHERE role='AE'"
        )).fetchall()
        sdr_quotas = conn.execute(text(
            "SELECT annual_quota FROM rep_config WHERE role='SDR'"
        )).fetchall()

    assert len(ae_quotas) >= 14
    for name, annual, quarterly in ae_quotas:
        assert annual is not None, f"{name} should have an annual_quota"
        assert quarterly == pytest.approx(annual / 4.0)

    # SDRs are non-carriers — annual_quota stays NULL (falsy 0 → NULL in seed).
    for (annual,) in sdr_quotas:
        assert annual is None


def test_seed_records_role_mix(clean_rep_config):
    _seed_main()
    engine = get_engine()
    with engine.connect() as conn:
        by_role = dict(conn.execute(text(
            "SELECT role, COUNT(*) FROM rep_config WHERE active=1 GROUP BY role"
        )).fetchall())
    assert by_role.get("AE", 0) >= 14
    assert by_role.get("SDR", 0) >= 10
    assert by_role.get("SDR Team Lead", 0) >= 2
    assert by_role.get("Manager", 0) >= 1
