"""8-flag risk taxonomy — each flag independently triggered, then composed."""
from __future__ import annotations

from datetime import date
from typing import Any

from agents.slt_metrics.forecast import risk_flags as rf
from agents.slt_metrics.types import ContactRole, OppRecord


TODAY = date(2026, 4, 13)


def _opp(**overrides: Any) -> OppRecord:
    base = dict(
        id="0061xABC",
        name="Test Opp",
        account_id="0011xACC", account_name="Acme Diner", account_website=None, account_type=None,
        owner_id="0051xREP", owner_name="Sofia Chen", owner_role="AE", owner_manager="Nate Lourens",
        stage="Proposal",
        is_closed=False, is_won=False,
        amount=120_000.0, acv=120_000.0, fixed_arr=None,
        locations=30, type="New Business", lead_source="Inbound",
        close_date=date(2026, 5, 13),
        created_date=None,
        last_activity_date=date(2026, 4, 11),
        last_modified_date=None,
        last_stage_change_date=date(2026, 4, 11),
        days_since_stage_change=2, time_in_stage=10, probability_sf=None,
        description=None, next_steps=None, next_step_date=None,
        icp_score=0.85, segment="MM",
        products={"Balance": 3},
        contact_roles=[ContactRole(
            contact_id="0031xCON", name="Buyer Bob", email="bob@acme.com",
            title="VP Ops", role="Economic Buyer", is_primary=True,
        )],
        raw={},
    )
    base.update(overrides)
    return OppRecord(**base)


def test_healthy_opp_returns_no_flags():
    assert rf.compute_risk_flags(_opp(), today=TODAY) == []


def test_stage_mismatch_late_phase_stalled():
    flags = rf.compute_risk_flags(_opp(stage="Proposal", time_in_stage=90), today=TODAY)
    assert "STAGE_MISMATCH" in flags


def test_stage_mismatch_doesnt_fire_mid_phase():
    flags = rf.compute_risk_flags(_opp(stage="Demo", time_in_stage=90), today=TODAY)
    assert "STAGE_MISMATCH" not in flags


def test_no_engagement_after_thirty_days():
    flags = rf.compute_risk_flags(_opp(last_activity_date=date(2026, 3, 1)), today=TODAY)
    assert "NO_ENGAGEMENT" in flags


def test_no_engagement_boundary_exactly_thirty_days():
    flags = rf.compute_risk_flags(_opp(last_activity_date=date(2026, 3, 14)), today=TODAY)
    assert "NO_ENGAGEMENT" not in flags  # exactly 30 → not > 30


def test_enterprise_stall_triggers_on_ent_late_silence():
    flags = rf.compute_risk_flags(
        _opp(segment="ENT", stage="Proposal", last_activity_date=date(2026, 2, 10)),
        today=TODAY,
    )
    assert "ENTERPRISE_STALL" in flags


def test_enterprise_stall_doesnt_fire_mm_segment():
    flags = rf.compute_risk_flags(
        _opp(segment="MM", stage="Proposal", last_activity_date=date(2026, 2, 10)),
        today=TODAY,
    )
    assert "ENTERPRISE_STALL" not in flags


def test_enterprise_stall_doesnt_fire_early_phase():
    flags = rf.compute_risk_flags(
        _opp(segment="ENT", stage="Demo", last_activity_date=date(2026, 2, 10)),
        today=TODAY,
    )
    assert "ENTERPRISE_STALL" not in flags


def test_zombie_needs_ninety_days_no_activity_and_no_stage_change():
    flags = rf.compute_risk_flags(
        _opp(
            last_activity_date=date(2026, 1, 1),
            last_stage_change_date=date(2026, 1, 1),
            days_since_stage_change=102,
        ),
        today=TODAY,
    )
    assert "ZOMBIE" in flags


def test_zombie_skips_when_recent_stage_change():
    flags = rf.compute_risk_flags(
        _opp(
            last_activity_date=date(2026, 1, 1),
            last_stage_change_date=date(2026, 4, 1),
            days_since_stage_change=12,
        ),
        today=TODAY,
    )
    assert "ZOMBIE" not in flags


def test_orphaned_no_owner():
    flags = rf.compute_risk_flags(_opp(owner_id=None, owner_name=None), today=TODAY)
    assert "ORPHANED" in flags


def test_orphaned_no_contact_roles():
    flags = rf.compute_risk_flags(_opp(contact_roles=[]), today=TODAY)
    assert "ORPHANED" in flags


def test_acv_missing_null():
    flags = rf.compute_risk_flags(_opp(acv=None), today=TODAY)
    assert "ACV_MISSING" in flags


def test_acv_missing_zero():
    flags = rf.compute_risk_flags(_opp(acv=0.0), today=TODAY)
    assert "ACV_MISSING" in flags


def test_no_products_empty_dict():
    flags = rf.compute_risk_flags(_opp(products={}), today=TODAY)
    assert "NO_PRODUCTS" in flags


def test_no_products_all_zero_counts():
    flags = rf.compute_risk_flags(_opp(products={"Balance": 0, "Guard": 0}), today=TODAY)
    assert "NO_PRODUCTS" in flags


def test_rep_risk_requires_owner_in_watchlist():
    flags = rf.compute_risk_flags(
        _opp(), today=TODAY, rep_risk_owners=frozenset({"Sofia Chen"})
    )
    assert "REP_RISK" in flags


def test_rep_risk_absent_when_owner_not_in_watchlist():
    flags = rf.compute_risk_flags(
        _opp(), today=TODAY, rep_risk_owners=frozenset({"Other AE"})
    )
    assert "REP_RISK" not in flags


def test_flags_compose_multiple_at_once():
    bad = _opp(
        owner_id=None, owner_name=None,        # ORPHANED (no owner)
        acv=None,                               # ACV_MISSING
        products={},                            # NO_PRODUCTS
        last_activity_date=date(2026, 1, 1),    # NO_ENGAGEMENT
    )
    flags = rf.compute_risk_flags(bad, today=TODAY)
    assert set(flags) >= {"NO_ENGAGEMENT", "ORPHANED", "ACV_MISSING", "NO_PRODUCTS"}


def test_null_last_activity_does_not_trigger_no_engagement():
    # Without an anchor date, we can't compute days elapsed — silent, not flagged.
    flags = rf.compute_risk_flags(_opp(last_activity_date=None), today=TODAY)
    assert "NO_ENGAGEMENT" not in flags


def test_future_activity_date_clamped_to_zero_days():
    # SF sometimes emits future dates; shouldn't trip NO_ENGAGEMENT.
    future = _opp(last_activity_date=date(2026, 4, 20))
    flags = rf.compute_risk_flags(future, today=TODAY)
    assert "NO_ENGAGEMENT" not in flags
