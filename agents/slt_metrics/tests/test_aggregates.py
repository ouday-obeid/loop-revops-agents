"""Pure-data aggregator tests — no I/O, no DB, no SF."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from agents.slt_metrics.excel_model import aggregates
from agents.slt_metrics.types import OppRecord


def _opp(**overrides: Any) -> OppRecord:
    base: dict[str, Any] = dict(
        id="0061x000000001",
        name="Opp",
        account_id=None,
        account_name=None,
        account_website=None,
        account_type=None,
        owner_id=None,
        owner_name=None,
        owner_role=None,
        owner_manager=None,
        stage="Demo",
        is_closed=False,
        is_won=False,
        amount=None,
        acv=50_000.0,
        fixed_arr=None,
        locations=None,
        type="New Business",
        lead_source="Inbound",
        close_date=date(2026, 4, 20),
        created_date=datetime(2026, 1, 15, 10, 0),
        last_activity_date=None,
        last_modified_date=None,
        last_stage_change_date=None,
        days_since_stage_change=None,
        time_in_stage=None,
        probability_sf=None,
        description=None,
        next_steps=None,
        next_step_date=None,
        icp_score=None,
        segment="MM",
        products={},
        contact_roles=[],
        raw={},
    )
    base.update(overrides)
    return OppRecord(**base)


# ---------------------------------------------------------------- classify_opp_kind

def test_classify_opp_kind_new_business():
    assert aggregates.classify_opp_kind("New Business") == "new_biz"
    assert aggregates.classify_opp_kind("new business") == "new_biz"


def test_classify_opp_kind_expansion_variants():
    for t in ("Expansion", "Upsell", "Existing Customer - Upgrade", "Renewal"):
        assert aggregates.classify_opp_kind(t) == "expansion", t


def test_classify_opp_kind_other_and_null():
    assert aggregates.classify_opp_kind(None) == "other"
    assert aggregates.classify_opp_kind("") == "other"
    assert aggregates.classify_opp_kind("Services") == "other"


# ---------------------------------------------------------------- monthly_closed_won_by_kind

def test_monthly_closed_won_splits_new_biz_and_expansion():
    closed = [
        _opp(is_closed=True, is_won=True, type="New Business",
             close_date=date(2026, 4, 10), acv=100_000.0),
        _opp(is_closed=True, is_won=True, type="Expansion",
             close_date=date(2026, 4, 25), acv=40_000.0),
        _opp(is_closed=True, is_won=True, type="New Business",
             close_date=date(2026, 5, 3), acv=60_000.0),
    ]
    out = aggregates.monthly_closed_won_by_kind(closed)
    assert out[4] == {"new_biz": 100_000.0, "expansion": 40_000.0, "other": 0.0}
    assert out[5] == {"new_biz": 60_000.0, "expansion": 0.0, "other": 0.0}


def test_monthly_closed_won_ignores_losses_and_null_dates():
    closed = [
        _opp(is_closed=True, is_won=False, close_date=date(2026, 4, 10), acv=99_999.0),
        _opp(is_closed=True, is_won=True, close_date=None, acv=50_000.0),
        _opp(is_closed=True, is_won=True, close_date=date(2026, 4, 20), acv=10_000.0),
    ]
    out = aggregates.monthly_closed_won_by_kind(closed)
    assert out == {4: {"new_biz": 10_000.0, "expansion": 0.0, "other": 0.0}}


def test_monthly_closed_won_empty_input():
    assert aggregates.monthly_closed_won_by_kind([]) == {}


# ---------------------------------------------------------------- monthly_opps_created

def test_monthly_opps_created_buckets_by_year_month():
    opps = [
        _opp(created_date=datetime(2026, 1, 15)),
        _opp(created_date=datetime(2026, 1, 28)),
        _opp(created_date=datetime(2026, 2, 3)),
        _opp(created_date=datetime(2025, 12, 20)),
    ]
    out = aggregates.monthly_opps_created(opps)
    assert out == {(2026, 1): 2, (2026, 2): 1, (2025, 12): 1}


def test_monthly_opps_created_skips_null_dates():
    opps = [_opp(created_date=None), _opp(created_date=datetime(2026, 3, 1))]
    assert aggregates.monthly_opps_created(opps) == {(2026, 3): 1}


# ---------------------------------------------------------------- stage_distribution

def test_stage_distribution_only_counts_open_opps():
    opps = [
        _opp(stage="Proposal", is_closed=False, acv=100_000.0),
        _opp(stage="Proposal", is_closed=False, acv=50_000.0),
        _opp(stage="Demo", is_closed=False, acv=25_000.0),
        _opp(stage="Closed Won", is_closed=True, is_won=True, acv=999_999.0),
    ]
    out = aggregates.stage_distribution(opps)
    assert "Closed Won" not in out
    assert out["Proposal"]["count"] == 2
    assert out["Proposal"]["acv"] == 150_000.0
    assert out["Demo"]["count"] == 1
    total = 150_000.0 + 25_000.0
    assert abs(out["Proposal"]["pct_of_pipeline"] - (150_000.0 / total)) < 1e-9
    assert abs(out["Demo"]["pct_of_pipeline"] - (25_000.0 / total)) < 1e-9


def test_stage_distribution_handles_missing_stage():
    opps = [_opp(stage="", is_closed=False, acv=10_000.0)]
    out = aggregates.stage_distribution(opps)
    assert "(unknown)" in out


def test_stage_distribution_zero_pipeline_zero_pct():
    opps = [_opp(stage="Demo", is_closed=False, acv=None)]
    out = aggregates.stage_distribution(opps)
    assert out["Demo"]["count"] == 1
    assert out["Demo"]["acv"] == 0.0
    assert out["Demo"]["pct_of_pipeline"] == 0.0


# ---------------------------------------------------------------- quarterly_closed_by_segment

def test_quarterly_closed_by_segment_splits_won_and_lost():
    closed = [
        _opp(is_closed=True, is_won=True,  segment="MM",  acv=90_000.0),
        _opp(is_closed=True, is_won=True,  segment="MM",  acv=110_000.0),
        _opp(is_closed=True, is_won=False, segment="MM",  acv=50_000.0),
        _opp(is_closed=True, is_won=True,  segment="ENT", acv=400_000.0),
    ]
    out = aggregates.quarterly_closed_by_segment(closed)
    assert out["MM"] == {
        "won_count": 2, "won_acv": 200_000.0,
        "lost_count": 1, "lost_acv": 50_000.0,
    }
    assert out["ENT"]["won_count"] == 1
    assert out["ENT"]["won_acv"] == 400_000.0


def test_quarterly_closed_by_segment_falls_back_to_acv_band():
    closed = [
        _opp(is_closed=True, is_won=True, segment=None, acv=5_000.0),     # SMB band
        _opp(is_closed=True, is_won=True, segment=None, acv=200_000.0),   # ENT band
    ]
    out = aggregates.quarterly_closed_by_segment(closed)
    assert out["SMB"]["won_count"] == 1
    assert out["ENT"]["won_count"] == 1


def test_quarterly_closed_by_segment_unassigned_when_no_segment_and_no_acv():
    closed = [_opp(is_closed=True, is_won=False, segment=None, acv=None)]
    out = aggregates.quarterly_closed_by_segment(closed)
    assert "Unassigned" in out
    assert out["Unassigned"]["lost_count"] == 1


# ---------------------------------------------------------------- lead_source_summary

def test_lead_source_summary_computes_win_rate_and_sorts_by_count():
    opps = [
        _opp(lead_source="Inbound"),
        _opp(lead_source="Inbound"),
        _opp(lead_source="Inbound", is_closed=True, is_won=True, acv=50_000.0),
        _opp(lead_source="Outbound"),
        _opp(lead_source="Outbound", is_closed=True, is_won=True, acv=75_000.0),
        _opp(lead_source=None),
    ]
    out = aggregates.lead_source_summary(opps)
    assert out[0]["source"] == "Inbound"
    assert out[0]["count"] == 3
    assert out[0]["won"] == 1
    assert out[0]["won_acv"] == 50_000.0
    assert abs(out[0]["win_rate"] - (1 / 3)) < 1e-9
    assert out[1]["source"] == "Outbound"
    assert out[1]["win_rate"] == 0.5
    unknown = next(r for r in out if r["source"] == "(unknown)")
    assert unknown["count"] == 1
    assert unknown["win_rate"] == 0.0


def test_lead_source_summary_empty_input():
    assert aggregates.lead_source_summary([]) == []
