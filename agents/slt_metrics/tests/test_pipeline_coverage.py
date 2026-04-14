"""Pipeline coverage by segment."""
from __future__ import annotations

import pytest

from agents.slt_metrics.board_metrics import pipeline_coverage
from agents.slt_metrics.types import ScoredDeal


def _deal(*, opp_id: str, acv: float, segment: str | None) -> ScoredDeal:
    return ScoredDeal(
        opp_id=opp_id, opp_name=f"Opp {opp_id}",
        owner_name=None, account_name=None, segment=segment,
        stage="Pilot", amount=acv, acv=acv, close_date=None,
        score=50, probability=0.5, category="Commit",
        weighted_acv=acv * 0.5, pillars={}, risk_flags=[],
        weights_version="v1-seed",
    )


def test_segment_explicit_wins_over_acv_band():
    deals = [
        _deal(opp_id="D1", acv=20_000.0, segment="ENT"),    # ENT despite SMB-sized ACV
    ]
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals, quotas_by_segment={"ENT": 10_000.0},
    )
    assert report.by_segment["ENT"].open_pipeline == pytest.approx(20_000.0)
    assert report.by_segment["SMB"].open_pipeline == 0.0


def test_missing_segment_falls_back_to_acv_band():
    # acv=100_000 → MM band; acv=300_000 → ENT.
    deals = [
        _deal(opp_id="D1", acv=100_000.0, segment=None),
        _deal(opp_id="D2", acv=300_000.0, segment=None),
    ]
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals, quotas_by_segment={},
    )
    assert report.by_segment["MM"].open_pipeline == pytest.approx(100_000.0)
    assert report.by_segment["ENT"].open_pipeline == pytest.approx(300_000.0)


def test_coverage_ratio_and_meets_target():
    # MM pipeline 900k vs quota 300k → 3.0 coverage (meets 3x target).
    deals = [_deal(opp_id="D1", acv=900_000.0, segment="MM")]
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals, quotas_by_segment={"MM": 300_000.0},
    )
    mm = report.by_segment["MM"]
    assert mm.coverage_ratio == pytest.approx(3.0)
    assert mm.target_ratio == 3.0
    assert mm.meets_target is True


def test_coverage_below_target_fails_gate():
    deals = [_deal(opp_id="D1", acv=800_000.0, segment="ENT")]
    # ENT quota 500k → ratio 1.6, below 4x target.
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals, quotas_by_segment={"ENT": 500_000.0},
    )
    ent = report.by_segment["ENT"]
    assert ent.coverage_ratio == pytest.approx(1.6)
    assert ent.meets_target is False


def test_meets_target_none_when_quota_missing():
    deals = [_deal(opp_id="D1", acv=100_000.0, segment="MM")]
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals, quotas_by_segment={},
    )
    mm = report.by_segment["MM"]
    assert mm.coverage_ratio is None
    assert mm.meets_target is None


def test_unassigned_bucket_for_segmentless_null_acv():
    deals = [_deal(opp_id="D1", acv=0.0, segment=None)]
    # acv=0 falls in SMB band (0.0–25k), so actually SMB not Unassigned.
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals, quotas_by_segment={},
    )
    assert report.by_segment["SMB"].open_pipeline == 0.0


def test_shortcuts_populate_from_by_segment():
    deals = [
        _deal(opp_id="D1", acv=900_000.0, segment="MM"),
        _deal(opp_id="D2", acv=1_200_000.0, segment="ENT"),
    ]
    report = pipeline_coverage.build_coverage_report(
        scored_deals=deals,
        quotas_by_segment={"MM": 300_000.0, "ENT": 300_000.0},
    )
    assert report.mm_coverage == pytest.approx(3.0)
    assert report.ent_coverage == pytest.approx(4.0)
    assert report.smb_coverage is None  # no SMB deals → no quota → None
