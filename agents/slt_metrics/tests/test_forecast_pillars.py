"""Forecast pillars — ICP, Stage, Activity.

Tests are pure-python — no DB, no MCPs — because pillars are intentionally
side-effect-free so the backtest replay script can call them in a tight loop.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from agents.slt_metrics.forecast import pillars
from agents.slt_metrics.pipeline.config import ICP_PROXY_CAP
from agents.slt_metrics.types import OppRecord


def _opp(**overrides: Any) -> OppRecord:
    base = dict(
        id="0061xABC",
        name="Test Opp",
        account_id=None, account_name=None, account_website=None, account_type=None,
        owner_id=None, owner_name=None, owner_role=None, owner_manager=None,
        stage="Demo",
        is_closed=False, is_won=False,
        amount=None, acv=None, fixed_arr=None,
        locations=None, type=None, lead_source=None,
        close_date=None, created_date=None,
        last_activity_date=None, last_modified_date=None,
        last_stage_change_date=None, days_since_stage_change=None,
        time_in_stage=None, probability_sf=None,
        description=None, next_steps=None, next_step_date=None,
        icp_score=None, segment=None,
        products={}, contact_roles=[], raw={},
    )
    base.update(overrides)
    return OppRecord(**base)


TODAY = date(2026, 4, 13)


# ------------------------------------------------------------------ ICP

def test_icp_from_sf_score_unit_range():
    p = pillars.icp(_opp(icp_score=0.85))
    assert p.value == 0.85
    assert "sf-icp-score" in p.detail


def test_icp_from_sf_score_percent_range_normalized():
    p = pillars.icp(_opp(icp_score=87.0))
    assert p.value == pytest.approx(0.87)
    assert "sf-icp-score" in p.detail


def test_icp_from_sf_score_ten_scale_normalized():
    p = pillars.icp(_opp(icp_score=8.5))
    assert p.value == pytest.approx(0.85)


def test_icp_proxy_capped_for_enterprise_opp():
    opp = _opp(
        icp_score=None,
        locations=100,
        acv=500_000.0,
        segment="ENT",
        lead_source="Inbound",
    )
    p = pillars.icp(opp)
    # Raw proxy would hit 1.0 here; cap keeps it at 0.5.
    assert p.value == ICP_PROXY_CAP
    assert "proxy-capped" in p.detail


def test_icp_proxy_below_cap_reports_uncapped_detail():
    opp = _opp(
        icp_score=None,
        locations=10, acv=30_000.0,
        segment="SMB", lead_source="Outbound",
    )
    p = pillars.icp(opp)
    assert 0.0 < p.value < ICP_PROXY_CAP
    assert "proxy-capped" not in p.detail


def test_icp_proxy_zero_when_all_signals_missing():
    opp = _opp(icp_score=None)
    p = pillars.icp(opp)
    assert p.value == 0.0


def test_icp_out_of_range_clamped():
    assert pillars.icp(_opp(icp_score=-5.0)).value == 0.0
    assert pillars.icp(_opp(icp_score=999.0)).value == 1.0


# ------------------------------------------------------------------ Stage

def test_stage_pillar_proposal_is_full():
    p = pillars.stage(_opp(stage="Proposal"))
    assert p.value == 1.0
    assert "Proposal" in p.detail


def test_stage_pillar_demo_is_mid():
    p = pillars.stage(_opp(stage="Demo"))
    assert p.value == pytest.approx(10 / 25)


def test_stage_pillar_new_meeting_set_is_low():
    p = pillars.stage(_opp(stage="New Meeting Set"))
    assert p.value == pytest.approx(5 / 25)


def test_stage_pillar_terminal_losses_are_zero():
    for terminal in ("No Show", "Disqualified", "Closed Lost"):
        assert pillars.stage(_opp(stage=terminal)).value == 0.0


def test_stage_pillar_unknown_stage():
    p = pillars.stage(_opp(stage="Flirty Whisper"))
    assert p.value == 0.0
    assert "unknown-stage" in p.detail


# ------------------------------------------------------------------ Activity

def test_activity_recent_touch_bonus():
    # 2 days ago → base 25 + bonus 5 → capped at 25 / 25 = 1.0.
    p = pillars.activity(_opp(last_activity_date=date(2026, 4, 11), stage="Demo"), today=TODAY)
    assert p.value == 1.0
    assert "+5recent" in p.detail


def test_activity_seven_day_boundary_inclusive():
    # Exactly 7 days → still in the <=7 band (score 25). No recent bonus (>3d).
    p = pillars.activity(_opp(last_activity_date=date(2026, 4, 6), stage="Demo"), today=TODAY)
    assert p.value == 1.0  # 25 / 25, no penalty, no bonus at 7d


def test_activity_mid_band_cuts_in():
    # 21 days → 15/25
    p = pillars.activity(_opp(last_activity_date=date(2026, 3, 23), stage="Demo"), today=TODAY)
    assert p.value == pytest.approx(15 / 25)


def test_activity_null_last_activity_scores_zero():
    p = pillars.activity(_opp(last_activity_date=None, stage="Demo"), today=TODAY)
    assert p.value == 0.0
    assert p.detail == "no-activity"


def test_activity_silence_penalty_applies_only_in_late_phase():
    # 70 days silence, Demo (mid-phase) → base 0, no penalty.
    demo = pillars.activity(
        _opp(last_activity_date=date(2026, 2, 2), stage="Demo"), today=TODAY
    )
    # 70 days silence, Proposal (late) → base 0, penalty clamped to 0.
    proposal = pillars.activity(
        _opp(last_activity_date=date(2026, 2, 2), stage="Proposal"), today=TODAY
    )
    assert demo.value == 0.0
    assert proposal.value == 0.0
    assert "silence" in proposal.detail  # penalty applied visibly
    assert "silence" not in demo.detail


def test_activity_negative_days_treated_as_zero():
    # SF date in the future → still "recent".
    future = _opp(last_activity_date=date(2026, 4, 20), stage="Demo")
    p = pillars.activity(future, today=TODAY)
    assert p.value == 1.0


def test_activity_detail_shows_days_and_base():
    p = pillars.activity(_opp(last_activity_date=date(2026, 4, 2), stage="Demo"), today=TODAY)
    # 11 days → 20/25 band
    assert "11d" in p.detail
    assert "base=20" in p.detail


def test_activity_upper_band_sixty_days():
    p = pillars.activity(
        _opp(last_activity_date=date(2026, 2, 12), stage="Demo"),
        today=TODAY,
    )
    # 60 days → 2/25 band (mid-phase so no penalty)
    assert p.value == pytest.approx(2 / 25)


def test_activity_above_sixty_no_penalty_mid_phase():
    p = pillars.activity(
        _opp(last_activity_date=date(2026, 2, 10), stage="Demo"),
        today=TODAY,
    )
    assert p.value == 0.0  # >60d → 0 band, no penalty (not Late)


# ------------------------------------------------------------------ Timeline

def test_timeline_missing_close_date_is_zero():
    p = pillars.timeline(_opp(close_date=None, stage="Demo"), today=TODAY)
    assert p.value == 0.0
    assert "no-close-date" in p.detail


def test_timeline_past_due_scores_floor():
    p = pillars.timeline(
        _opp(close_date=date(2026, 4, 1), stage="Proposal"),  # 12d past TODAY
        today=TODAY,
    )
    assert p.value == pytest.approx(0.2)
    assert "past-due" in p.detail


def test_timeline_near_with_valid_late_stage_is_full():
    p = pillars.timeline(
        _opp(close_date=date(2026, 4, 30), stage="Proposal"),  # 17d out
        today=TODAY,
    )
    assert p.value == pytest.approx(1.0)
    assert "near" in p.detail


def test_timeline_near_with_early_stage_gets_penalty():
    # 10 days out but stage is Demo (not in NEAR_VALID) → floor at 0.6
    p = pillars.timeline(
        _opp(close_date=date(2026, 4, 23), stage="Demo"),
        today=TODAY,
    )
    assert p.value == pytest.approx(0.6)
    assert "early" in p.detail


def test_timeline_mid_band_linear_ramp():
    # ~60 days out in Demo → mid band linear — halfway between 1.0 and 0.6 = 0.8
    p = pillars.timeline(
        _opp(close_date=date(2026, 6, 12), stage="Demo"),  # 60d from TODAY
        today=TODAY,
    )
    assert p.value == pytest.approx(0.8)
    assert "mid" in p.detail


def test_timeline_mid_band_endpoint_lower_bound():
    # Exactly 90d out → TIMELINE_MID_LOW_SCORE (0.6)
    p = pillars.timeline(
        _opp(close_date=date(2026, 7, 12), stage="Demo"),  # 90d
        today=TODAY,
    )
    assert p.value == pytest.approx(0.6)


def test_timeline_far_is_flat():
    # 120 days out → flat TIMELINE_FAR_LOW_SCORE (0.4)
    p = pillars.timeline(
        _opp(close_date=date(2026, 8, 11), stage="Demo"),  # 120d
        today=TODAY,
    )
    assert p.value == pytest.approx(0.4)
    assert "far" in p.detail


def test_timeline_late_phase_stall_penalty():
    # 40d out, Proposal (Late), stalled 90d in stage → 1.0 − 0.3 = 0.7.
    # mid band at 40d → linear: (40-30)/60 = 0.167 fraction → 1.0 − 0.167 * 0.4 = 0.933
    # then − 0.3 = 0.633
    p = pillars.timeline(
        _opp(close_date=date(2026, 5, 23), stage="Proposal", time_in_stage=90),
        today=TODAY,
    )
    assert p.value == pytest.approx(0.6333, abs=1e-3)
    assert "stall90d" in p.detail


def test_timeline_stall_only_applies_to_late_phase():
    # Same 90d stall but stage is Demo (mid phase) → no penalty
    p = pillars.timeline(
        _opp(close_date=date(2026, 5, 23), stage="Demo", time_in_stage=90),
        today=TODAY,
    )
    assert "stall" not in p.detail


def test_timeline_stall_below_threshold_no_penalty():
    # 30d stalled but threshold is >60 → no penalty
    p = pillars.timeline(
        _opp(close_date=date(2026, 5, 23), stage="Proposal", time_in_stage=30),
        today=TODAY,
    )
    assert "stall" not in p.detail


# ------------------------------------------------------------------ Call (stub)

def test_call_stub_returns_zero():
    p = pillars.call(_opp(), today=TODAY)
    assert p.value == 0.0
    assert "call-stub" in p.detail


def test_all_pillars_order_matches_weights():
    from agents.slt_metrics.types import ForecastWeights

    pillar_names = tuple(pillars.all_pillars())
    weight_fields = tuple(f for f in ForecastWeights.__dataclass_fields__ if f != "version")
    assert pillar_names == weight_fields
