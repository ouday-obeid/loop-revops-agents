"""Sheet 6 — Pipeline by segment.

Shows the per-segment rollup (commit / best-case / weighted) from
`forecast_rollup.by_segment`, alongside open-pipeline totals derived from
`scored_deals`.
"""
from __future__ import annotations

from collections import defaultdict

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "Segment", "Deal Count",
    "Open Pipeline (ACV)", "Commit", "Best Case", "Weighted",
)

_FORMATS = (
    None, S.FMT_INT,
    S.FMT_MONEY, S.FMT_MONEY, S.FMT_MONEY, S.FMT_MONEY,
)

_SEGMENT_ORDER = ("ENT", "MM", "SMB", "Unassigned")


def _commit_of(vals: dict) -> float:
    return float(vals.get("commit_amount", vals.get("commit", 0.0)) or 0.0)


def _best_of(vals: dict) -> float:
    return float(vals.get("best_case_amount", vals.get("best_case", 0.0)) or 0.0)


def _weighted_of(vals: dict) -> float:
    return float(vals.get("weighted_amount", vals.get("weighted", 0.0)) or 0.0)


class PipelineSegmentSheet(BaseSheet):
    sheet_name = "Pipeline by Segment"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Pipeline by Segment · {payload.horizon_quarter}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        count_by_seg: dict[str, int] = defaultdict(int)
        pipe_by_seg: dict[str, float] = defaultdict(float)
        for deal in payload.scored_deals:
            seg = deal.segment or "Unassigned"
            count_by_seg[seg] += 1
            if deal.acv is not None:
                pipe_by_seg[seg] += deal.acv

        rollup_by_seg = payload.forecast_rollup.by_segment

        # Stable ordering: canonical ENT → MM → SMB → Unassigned, then any extras alphabetically.
        seen = set(_SEGMENT_ORDER)
        extras = sorted(k for k in (count_by_seg.keys() | rollup_by_seg.keys()) if k not in seen)
        segments = [s for s in _SEGMENT_ORDER if s in (count_by_seg.keys() | rollup_by_seg.keys())] + extras

        for i, seg in enumerate(segments, start=3):
            seg_rollup = rollup_by_seg.get(seg, {})
            H.write_body_row(
                ws,
                row=i,
                values=(
                    seg,
                    count_by_seg.get(seg, 0),
                    pipe_by_seg.get(seg, 0.0),
                    _commit_of(seg_rollup),
                    _best_of(seg_rollup),
                    _weighted_of(seg_rollup),
                ),
                number_formats=list(_FORMATS),
            )

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
