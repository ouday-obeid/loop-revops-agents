"""Pipeline coverage — open pipeline Σ ACV / quota, by segment.

Target ratios (scoping doc §Appendix C):
  - SMB  → 3x
  - MM   → 3x
  - ENT  → 4x

We emit both the computed ratios AND a pass/fail flag per segment so the
Board Metrics sheet can color-code without re-running the math.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from agents.slt_metrics.pipeline.config import COVERAGE_TARGETS, segment_for_acv
from agents.slt_metrics.types import ScoredDeal


@dataclass
class SegmentCoverage:
    segment: str
    open_pipeline: float
    quota: float | None
    coverage_ratio: float | None
    target_ratio: float
    meets_target: bool | None


@dataclass
class CoverageReport:
    by_segment: dict[str, SegmentCoverage] = field(default_factory=dict)
    mm_coverage: float | None = None
    ent_coverage: float | None = None
    smb_coverage: float | None = None


def build_coverage_report(
    *,
    scored_deals: Iterable[ScoredDeal],
    quotas_by_segment: Mapping[str, float],
) -> CoverageReport:
    """Σ open ACV per segment ÷ segment quota.

    Segment inference: use the deal's explicit `segment` field first, fall
    back to the ACV band in `pipeline.config.segment_for_acv`. Deals with
    neither land in an "Unassigned" bucket so they're still visible.
    """
    open_by_seg: dict[str, float] = {}
    for deal in scored_deals:
        seg = deal.segment or segment_for_acv(deal.acv) or "Unassigned"
        open_by_seg[seg] = open_by_seg.get(seg, 0.0) + (deal.acv or 0.0)

    report = CoverageReport()
    for seg, target in COVERAGE_TARGETS.items():
        pipeline = open_by_seg.get(seg, 0.0)
        quota = quotas_by_segment.get(seg)
        ratio = pipeline / quota if quota and quota > 0 else None
        report.by_segment[seg] = SegmentCoverage(
            segment=seg,
            open_pipeline=pipeline,
            quota=quota,
            coverage_ratio=ratio,
            target_ratio=target,
            meets_target=(ratio >= target) if ratio is not None else None,
        )
    # Unassigned bucket — visible but not part of the pass/fail grid.
    if "Unassigned" in open_by_seg:
        report.by_segment["Unassigned"] = SegmentCoverage(
            segment="Unassigned",
            open_pipeline=open_by_seg["Unassigned"],
            quota=None, coverage_ratio=None,
            target_ratio=0.0, meets_target=None,
        )

    # Shortcut accessors the Board Metrics sheet uses.
    report.mm_coverage = report.by_segment.get("MM", SegmentCoverage(
        segment="MM", open_pipeline=0.0, quota=None, coverage_ratio=None,
        target_ratio=COVERAGE_TARGETS["MM"], meets_target=None,
    )).coverage_ratio
    report.ent_coverage = report.by_segment.get("ENT", SegmentCoverage(
        segment="ENT", open_pipeline=0.0, quota=None, coverage_ratio=None,
        target_ratio=COVERAGE_TARGETS["ENT"], meets_target=None,
    )).coverage_ratio
    report.smb_coverage = report.by_segment.get("SMB", SegmentCoverage(
        segment="SMB", open_pipeline=0.0, quota=None, coverage_ratio=None,
        target_ratio=COVERAGE_TARGETS["SMB"], meets_target=None,
    )).coverage_ratio
    return report
