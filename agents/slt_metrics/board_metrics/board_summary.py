"""Compose a `BoardMetrics` payload from the upstream sub-reports.

This is the single function the Board Metrics sheet + board briefings call.
Keeps the composition logic outside of the sheet/briefing so the same
BoardMetrics drives both.
"""
from __future__ import annotations

from datetime import date

from agents.slt_metrics.board_metrics.arr_nrr import ArrNrrSnapshot
from agents.slt_metrics.board_metrics.pipeline_coverage import CoverageReport
from agents.slt_metrics.types import BoardMetrics, UnitEconomics


def build_board_metrics(
    *,
    as_of: date,
    arr_nrr: ArrNrrSnapshot,
    coverage: CoverageReport,
    unit_economics: UnitEconomics,
) -> BoardMetrics:
    """Assemble the BoardMetrics payload for the Excel sheet + briefings."""
    return BoardMetrics(
        as_of=as_of,
        arr=arr_nrr.arr,
        nrr=arr_nrr.nrr,
        logo_retention=arr_nrr.logo_retention,
        expansion_rate=arr_nrr.expansion_rate,
        pipeline_coverage_mm=coverage.mm_coverage,
        pipeline_coverage_ent=coverage.ent_coverage,
        unit_economics=unit_economics,
    )
