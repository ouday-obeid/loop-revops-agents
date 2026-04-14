"""ARR / NRR / logo retention snapshot."""
from __future__ import annotations

from datetime import date

import pytest

from agents.slt_metrics.board_metrics import arr_nrr
from agents.slt_metrics.types import OppRecord, UnitEconomics


def _won(
    *, opp_id: str, fixed_arr: float | None, acv: float | None = None,
    is_won: bool = True,
) -> OppRecord:
    return OppRecord(
        id=opp_id, name=f"Opp {opp_id}",
        account_id=None, account_name=None, account_website=None, account_type=None,
        owner_id=None, owner_name=None, owner_role=None, owner_manager=None,
        stage="Closed Won" if is_won else "Closed Lost",
        is_closed=True, is_won=is_won,
        amount=acv, acv=acv, fixed_arr=fixed_arr,
        locations=None, type=None, lead_source=None,
        close_date=date(2026, 3, 1), created_date=None, last_activity_date=None,
        last_modified_date=None, last_stage_change_date=None,
        days_since_stage_change=None, time_in_stage=None, probability_sf=None,
        description=None, next_steps=None, next_step_date=None,
        icp_score=None, segment=None,
    )


def _ue(*, gap: bool = False, nrr: float | None = None, logo: float | None = None,
        expansion: float | None = None) -> UnitEconomics:
    return UnitEconomics(
        gross_revenue_retention=None,
        net_revenue_retention=nrr,
        logo_retention=logo,
        expansion_rate=expansion,
        cac_payback_months=None,
        ltv_cac_ratio=None,
        gap_flag=gap,
    )


def test_arr_sums_fixed_arr_from_closed_won():
    opps = [
        _won(opp_id="W1", fixed_arr=100_000.0),
        _won(opp_id="W2", fixed_arr=200_000.0),
    ]
    snap = arr_nrr.build_arr_nrr(
        as_of=date(2026, 4, 1), closed_opps=opps, unit_economics=_ue(gap=True),
    )
    assert snap.arr == pytest.approx(300_000.0)


def test_arr_falls_back_to_acv_when_fixed_arr_missing():
    opps = [
        _won(opp_id="W1", fixed_arr=None, acv=50_000.0),
        _won(opp_id="W2", fixed_arr=100_000.0),
    ]
    snap = arr_nrr.build_arr_nrr(
        as_of=date(2026, 4, 1), closed_opps=opps, unit_economics=_ue(gap=True),
    )
    assert snap.arr == pytest.approx(150_000.0)


def test_arr_ignores_lost_opps():
    opps = [
        _won(opp_id="W1", fixed_arr=100_000.0),
        _won(opp_id="L1", fixed_arr=200_000.0, is_won=False),
    ]
    snap = arr_nrr.build_arr_nrr(
        as_of=date(2026, 4, 1), closed_opps=opps, unit_economics=_ue(gap=True),
    )
    assert snap.arr == pytest.approx(100_000.0)


def test_arr_none_when_no_wins_with_value():
    snap = arr_nrr.build_arr_nrr(
        as_of=date(2026, 4, 1), closed_opps=[], unit_economics=_ue(gap=True),
    )
    assert snap.arr is None


def test_nrr_prefers_bq_when_healthy():
    opps = [_won(opp_id="W1", fixed_arr=100_000.0)]
    snap = arr_nrr.build_arr_nrr(
        as_of=date(2026, 4, 1), closed_opps=opps,
        unit_economics=_ue(nrr=1.12, logo=0.92, expansion=0.20),
    )
    assert snap.nrr == 1.12
    assert snap.logo_retention == 0.92
    assert snap.expansion_rate == 0.20


def test_nrr_none_when_bq_gap_flagged():
    opps = [_won(opp_id="W1", fixed_arr=100_000.0)]
    snap = arr_nrr.build_arr_nrr(
        as_of=date(2026, 4, 1), closed_opps=opps,
        unit_economics=_ue(gap=True, nrr=999.9),  # gap_flag dominates
    )
    assert snap.nrr is None
    assert snap.logo_retention is None
    assert snap.expansion_rate is None
