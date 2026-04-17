"""Sanity tests for agents.slt_metrics.pipeline.planning."""
from __future__ import annotations

import pytest

from agents.slt_metrics.pipeline.planning import (
    AE_ROSTER,
    ANNUAL_TARGETS,
    BLENDED_TARGETS,
    HEADCOUNT,
    MANAGER_GROUPS,
    MONTHLY_TARGETS,
    QUARTERLY_FUNNEL_TARGETS,
    RATES,
    SDR_ROSTER,
    SEASONALITY,
    SEGMENTS,
    manager_for_ae,
    monthly_target,
    quarterly_funnel_target,
    segment_targets,
)


# ---------------------------------------------------------------- coverage

def test_monthly_targets_cover_twelve_months():
    assert set(MONTHLY_TARGETS.keys()) == set(range(1, 13))


def test_seasonality_covers_twelve_months():
    assert set(SEASONALITY.keys()) == set(range(1, 13))


def test_quarterly_funnel_targets_cover_all_quarters_and_segments():
    assert set(QUARTERLY_FUNNEL_TARGETS.keys()) == {"Q1", "Q2", "Q3", "Q4"}
    for q, row in QUARTERLY_FUNNEL_TARGETS.items():
        assert set(row.keys()) == {"SMB", "MM", "ENT"}


def test_segments_have_all_three_tiers():
    assert set(SEGMENTS.keys()) == {"SMB", "MM", "ENT"}


# ---------------------------------------------------------------- reconciliation

def test_segment_mix_sums_to_one():
    total = sum(s.mix_pct for s in SEGMENTS.values())
    assert abs(total - 1.0) < 0.01


def test_monthly_new_biz_sums_close_to_annual_gross_minus_expansion():
    # Monthly new_biz totals are the New Customer ARR ramp; expansion is
    # tracked separately. Loose tolerance — ramp is rounded in the source.
    nb_total = sum(m.new_biz for m in MONTHLY_TARGETS.values())
    # Should land near net_new_arr (17.08M). Within 2% tolerance.
    assert abs(nb_total - ANNUAL_TARGETS.net_new_arr) / ANNUAL_TARGETS.net_new_arr < 0.02


def test_monthly_expansion_sums_close_to_annual_expansion():
    exp_total = sum(m.expansion for m in MONTHLY_TARGETS.values())
    assert abs(exp_total - ANNUAL_TARGETS.expansion_arr) / ANNUAL_TARGETS.expansion_arr < 0.05


# ---------------------------------------------------------------- helpers

def test_monthly_target_new_biz_and_expansion():
    assert monthly_target(1, "new_biz") == pytest.approx(716_667.0)
    assert monthly_target(1, "expansion") == pytest.approx(90_032.0)
    assert monthly_target(12, "expansion") == pytest.approx(218_300.0)


def test_monthly_target_unknown_month_returns_zero():
    assert monthly_target(13) == 0.0
    assert monthly_target(0) == 0.0


def test_segment_targets_case_insensitive():
    assert segment_targets("smb") is SEGMENTS["SMB"]
    assert segment_targets("Ent") is SEGMENTS["ENT"]
    assert segment_targets("ENT") is SEGMENTS["ENT"]
    assert segment_targets("unknown") is None


def test_quarterly_funnel_target_tolerates_lowercase_segment():
    assert quarterly_funnel_target("Q1", "SMB") == pytest.approx(73.13)
    assert quarterly_funnel_target("Q1", "smb") == pytest.approx(73.13)
    assert quarterly_funnel_target("Q5", "SMB") == 0.0


# ---------------------------------------------------------------- roster integrity

def test_ae_roster_non_empty():
    ae_count = sum(1 for r in AE_ROSTER if r.role == "AE")
    assert ae_count >= 14  # 7 ramped + 7 ramping at time of port


def test_sdr_roster_non_empty():
    assert len(SDR_ROSTER) >= 12


def test_all_roster_names_unique():
    names = [r.name for r in AE_ROSTER] + [r.name for r in SDR_ROSTER]
    assert len(names) == len(set(names)), "roster has duplicate names"


def test_manager_groups_reference_real_ae_names():
    ae_names = {r.name for r in AE_ROSTER if r.role == "AE"}
    for mgr, members in MANAGER_GROUPS.items():
        for name in members:
            assert name in ae_names, f"{mgr} group references unknown AE {name!r}"


def test_manager_for_ae_returns_expected_manager():
    assert manager_for_ae("Alexis Marrero") == "Hutch"
    assert manager_for_ae("Simon Salomon") == "Nate"
    assert manager_for_ae("Alex Reyes") == "IC"
    assert manager_for_ae("Nobody") == "Unassigned"


# ---------------------------------------------------------------- shapes

def test_blended_targets_positive():
    assert BLENDED_TARGETS.arpl > 0
    assert BLENDED_TARGETS.ads > 0
    assert BLENDED_TARGETS.lpd > 0


def test_rates_sensible():
    assert 0.0 < RATES.win_rate_new_biz < 1.0
    assert 0.0 < RATES.win_rate_expansion <= 1.0
    assert RATES.opps_per_win > 1.0
    assert set(RATES.stage_win_rates.keys()) == {"Early", "Mid", "Late"}


def test_headcount_ramp_increases():
    assert HEADCOUNT.target > HEADCOUNT.starting
    assert HEADCOUNT.ae_target + HEADCOUNT.sdr_target <= HEADCOUNT.target
