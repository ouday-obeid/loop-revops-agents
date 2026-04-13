"""ForecastRollup — Commit / Best Case / Weighted totals + breakdowns."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from agents.slt_metrics.forecast import commit_best
from agents.slt_metrics.types import PillarScore, ScoredDeal


def _deal(
    *,
    opp_id: str = "0061xAAA",
    owner: str | None = "Sofia Chen",
    segment: str | None = "MM",
    acv: float = 100_000.0,
    score: int = 85,
    probability: float = 0.80,
) -> ScoredDeal:
    return ScoredDeal(
        opp_id=opp_id,
        opp_name="Test Opp",
        owner_name=owner,
        account_name="Acme",
        segment=segment,
        stage="Proposal",
        amount=acv,
        acv=acv,
        close_date=date(2026, 5, 13),
        score=score,
        probability=probability,
        category="Strong Commit",
        weighted_acv=acv * probability,
        pillars={},
        risk_flags=[],
        weights_version="v1-seed",
        raw=None,
    )


def test_roll_up_empty_returns_zeros():
    r = commit_best.roll_up([], horizon_quarter="FY-CURRENT")
    assert r.commit_amount == 0.0
    assert r.best_case_amount == 0.0
    assert r.weighted_amount == 0.0
    assert r.deal_count == 0
    assert r.horizon_quarter == "FY-CURRENT"


def test_roll_up_commit_threshold_is_eighty():
    deals = [
        _deal(opp_id="A", acv=100_000.0, score=80, probability=0.80),  # commits
        _deal(opp_id="B", acv=50_000.0,  score=79, probability=0.50),  # does not commit
    ]
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    assert r.commit_amount == 100_000.0
    assert r.best_case_amount == 150_000.0  # both ≥ 50
    assert r.weighted_amount == pytest.approx(100_000.0 * 0.80 + 50_000.0 * 0.50)


def test_roll_up_best_case_threshold_is_fifty():
    deals = [
        _deal(opp_id="A", acv=100_000.0, score=50, probability=0.50),
        _deal(opp_id="B", acv=75_000.0,  score=49, probability=0.22),  # excluded
    ]
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    assert r.best_case_amount == 100_000.0
    assert r.commit_amount == 0.0


def test_roll_up_weighted_includes_every_deal():
    deals = [
        _deal(opp_id="A", acv=100_000.0, score=20, probability=0.10),
        _deal(opp_id="B", acv=100_000.0, score=0,  probability=0.03),
    ]
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    assert r.weighted_amount == pytest.approx(100_000.0 * 0.10 + 100_000.0 * 0.03)
    assert r.commit_amount == 0.0
    assert r.best_case_amount == 0.0


def test_roll_up_by_owner_breakdown():
    deals = [
        _deal(opp_id="A", owner="Sofia", acv=100_000.0, score=85, probability=0.80),
        _deal(opp_id="B", owner="Sofia", acv=60_000.0,  score=55, probability=0.50),
        _deal(opp_id="C", owner="Marcus", acv=75_000.0, score=90, probability=0.80),
    ]
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    sofia = r.by_owner["Sofia"]
    marcus = r.by_owner["Marcus"]
    assert sofia["commit_amount"] == 100_000.0
    assert sofia["best_case_amount"] == 160_000.0
    assert sofia["deal_count"] == 2
    assert marcus["commit_amount"] == 75_000.0
    assert marcus["deal_count"] == 1


def test_roll_up_by_segment_breakdown():
    deals = [
        _deal(opp_id="A", segment="ENT", acv=500_000.0, score=85, probability=0.80),
        _deal(opp_id="B", segment="MM",  acv=60_000.0, score=85, probability=0.80),
        _deal(opp_id="C", segment="MM",  acv=40_000.0, score=85, probability=0.80),
    ]
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    assert r.by_segment["ENT"]["commit_amount"] == 500_000.0
    assert r.by_segment["MM"]["commit_amount"] == 100_000.0


def test_roll_up_null_owner_bucketed_as_unassigned():
    deals = [_deal(opp_id="A", owner=None, acv=100_000.0, score=85, probability=0.80)]
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    assert "unassigned" in r.by_owner
    assert r.by_owner["unassigned"]["commit_amount"] == 100_000.0


def test_roll_up_null_acv_contributes_zero_but_counted():
    deals = [_deal(opp_id="A", acv=0.0, score=90, probability=0.80)]
    # Use score >= commit threshold — still adds 0 to commit, but count bumps.
    r = commit_best.roll_up(deals, horizon_quarter="FY-CURRENT")
    assert r.commit_amount == 0.0
    assert r.deal_count == 1


def test_roll_up_horizon_quarter_passed_through():
    r = commit_best.roll_up(
        [_deal(opp_id="A")],
        horizon_quarter="FY2026-Q2",
    )
    assert r.horizon_quarter == "FY2026-Q2"
