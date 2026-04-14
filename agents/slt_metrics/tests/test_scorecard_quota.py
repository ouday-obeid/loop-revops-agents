"""rep_config CRUD + quarter math."""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from agents.slt_metrics.scorecards import quota
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _clean_rep_config():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM rep_config"))
    yield


def test_load_rep_quotas_empty_table_returns_empty_dict():
    assert quota.load_rep_quotas() == {}


def test_set_rep_quota_inserts_new_row():
    quota.set_rep_quota("Sofia Chen", role="AE", team="MM", quarterly_quota=300_000.0)
    assert quota.load_rep_quotas() == {"Sofia Chen": 300_000.0}


def test_set_rep_quota_updates_without_clobbering():
    quota.set_rep_quota(
        "Sofia Chen", role="AE", team="MM",
        quarterly_quota=300_000.0, attainment_floor_pct=0.80,
    )
    # Update only the quarterly_quota; other fields should persist.
    quota.set_rep_quota("Sofia Chen", quarterly_quota=350_000.0)
    rows = quota.load_all_rep_configs()
    r = next(r for r in rows if r.owner_name == "Sofia Chen")
    assert r.quarterly_quota == 350_000.0
    assert r.team == "MM"
    assert r.attainment_floor_pct == pytest.approx(0.80)


def test_load_rep_quotas_filters_inactive():
    quota.set_rep_quota("Active AE", role="AE", quarterly_quota=100_000.0, active=True)
    quota.set_rep_quota("Retired AE", role="AE", quarterly_quota=100_000.0, active=False)
    result = quota.load_rep_quotas()
    assert "Active AE" in result
    assert "Retired AE" not in result


def test_load_rep_quotas_filters_role():
    quota.set_rep_quota("AE One", role="AE", quarterly_quota=100_000.0)
    quota.set_rep_quota("SDR One", role="SDR", quarterly_quota=50_000.0)
    ae_only = quota.load_rep_quotas(role="AE")
    assert ae_only == {"AE One": 100_000.0}
    every = quota.load_rep_quotas(role=None)
    assert set(every.keys()) == {"AE One", "SDR One"}


def test_load_rep_quotas_skips_null_quota():
    quota.set_rep_quota("No Quota", role="AE")
    result = quota.load_rep_quotas()
    assert "No Quota" not in result


def test_load_all_rep_configs_returns_full_rows():
    quota.set_rep_quota(
        "Sofia Chen", role="AE", team="MM",
        quarterly_quota=300_000.0, annual_quota=1_200_000.0,
    )
    configs = quota.load_all_rep_configs()
    assert len(configs) == 1
    r = configs[0]
    assert r.role == "AE"
    assert r.team == "MM"
    assert r.annual_quota == 1_200_000.0
    assert r.active


def test_quarter_bounds_q1():
    s, e = quota.quarter_bounds(date(2026, 2, 15))
    assert s == date(2026, 1, 1)
    assert e == date(2026, 3, 31)


def test_quarter_bounds_q2():
    s, e = quota.quarter_bounds(date(2026, 4, 13))
    assert s == date(2026, 4, 1)
    assert e == date(2026, 6, 30)


def test_quarter_bounds_q4():
    s, e = quota.quarter_bounds(date(2026, 12, 31))
    assert s == date(2026, 10, 1)
    assert e == date(2026, 12, 31)


def test_quarter_elapsed_pct_start_of_quarter():
    pct = quota.quarter_elapsed_pct(date(2026, 4, 1))
    # First day of Q2 → 1 day elapsed of 91 total ≈ 0.011
    assert 0.0 < pct < 0.02


def test_quarter_elapsed_pct_end_of_quarter():
    pct = quota.quarter_elapsed_pct(date(2026, 6, 30))
    assert pct == pytest.approx(1.0)


def test_quarter_elapsed_pct_mid_quarter():
    pct = quota.quarter_elapsed_pct(date(2026, 5, 15))
    # Q2 = Apr1-Jun30 (91d). May15 is day 45 → 45/91 ≈ 0.49
    assert pct == pytest.approx(45 / 91, abs=1e-3)


def test_current_quarter_label():
    assert quota.current_quarter_label(date(2026, 4, 13)) == "FY2026-Q2"
    assert quota.current_quarter_label(date(2026, 1, 1)) == "FY2026-Q1"
    assert quota.current_quarter_label(date(2026, 10, 1)) == "FY2026-Q4"
