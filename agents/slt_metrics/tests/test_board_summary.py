"""Board summary composition."""
from __future__ import annotations

from datetime import date

from agents.slt_metrics.board_metrics import board_summary
from agents.slt_metrics.board_metrics.arr_nrr import ArrNrrSnapshot
from agents.slt_metrics.board_metrics.pipeline_coverage import (
    CoverageReport,
    SegmentCoverage,
)
from agents.slt_metrics.types import UnitEconomics


def _coverage(mm: float | None = 3.2, ent: float | None = 4.5) -> CoverageReport:
    report = CoverageReport()
    report.mm_coverage = mm
    report.ent_coverage = ent
    report.by_segment["MM"] = SegmentCoverage(
        segment="MM", open_pipeline=960_000.0, quota=300_000.0,
        coverage_ratio=mm, target_ratio=3.0, meets_target=(mm or 0) >= 3.0,
    )
    report.by_segment["ENT"] = SegmentCoverage(
        segment="ENT", open_pipeline=1_350_000.0, quota=300_000.0,
        coverage_ratio=ent, target_ratio=4.0, meets_target=(ent or 0) >= 4.0,
    )
    return report


def test_board_summary_populates_from_subreports():
    arr_snap = ArrNrrSnapshot(
        as_of=date(2026, 4, 1), arr=14_000_000.0,
        nrr=1.12, logo_retention=0.92, expansion_rate=0.20,
    )
    ue = UnitEconomics(
        gross_revenue_retention=0.95, net_revenue_retention=1.12,
        logo_retention=0.92, expansion_rate=0.20,
        cac_payback_months=14.0, ltv_cac_ratio=3.8,
        gap_flag=False,
    )
    coverage = _coverage(mm=3.2, ent=4.5)

    board = board_summary.build_board_metrics(
        as_of=date(2026, 4, 1),
        arr_nrr=arr_snap, coverage=coverage, unit_economics=ue,
    )
    assert board.as_of == date(2026, 4, 1)
    assert board.arr == 14_000_000.0
    assert board.nrr == 1.12
    assert board.logo_retention == 0.92
    assert board.expansion_rate == 0.20
    assert board.pipeline_coverage_mm == 3.2
    assert board.pipeline_coverage_ent == 4.5
    assert board.unit_economics.gap_flag is False
    assert board.unit_economics.cac_payback_months == 14.0


def test_board_summary_propagates_gap_flagged_unit_economics():
    arr_snap = ArrNrrSnapshot(
        as_of=date(2026, 4, 1), arr=10_000_000.0,
        nrr=None, logo_retention=None, expansion_rate=None,
    )
    ue = UnitEconomics(
        gross_revenue_retention=None, net_revenue_retention=None,
        logo_retention=None, expansion_rate=None,
        cac_payback_months=None, ltv_cac_ratio=None, gap_flag=True,
    )
    board = board_summary.build_board_metrics(
        as_of=date(2026, 4, 1),
        arr_nrr=arr_snap, coverage=_coverage(mm=None, ent=None),
        unit_economics=ue,
    )
    assert board.unit_economics.gap_flag is True
    assert board.pipeline_coverage_mm is None
    assert board.pipeline_coverage_ent is None
