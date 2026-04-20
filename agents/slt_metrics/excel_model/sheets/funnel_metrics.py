"""Sheet — Funnel Metrics.

Two blocks:
  1. Open-pipeline stage distribution (count / ACV / % of pipeline).
  2. Quarterly close performance by segment — won/lost counts, win rate,
     and coverage against `planning.QUARTERLY_FUNNEL_TARGETS` for the
     payload's current quarter.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import aggregates, helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.pipeline.config import STAGES
from agents.slt_metrics.pipeline.planning import quarterly_funnel_target
from agents.slt_metrics.types import RevenueModelPayload


_STAGE_HEADERS = ("Stage", "Open Count", "Open ACV", "% of Pipeline")
_STAGE_FORMATS = (None, S.FMT_INT, S.FMT_MONEY, S.FMT_PCT)

_SEGMENT_HEADERS = (
    "Segment",
    "Won Count", "Won ACV", "Lost Count", "Lost ACV",
    "Win Rate",
    "Target Deals", "Closed Count", "Coverage",
)
_SEGMENT_FORMATS = (
    None,
    S.FMT_INT, S.FMT_MONEY, S.FMT_INT, S.FMT_MONEY,
    S.FMT_PCT,
    S.FMT_RATIO, S.FMT_INT, S.FMT_PCT,
)

_SEGMENT_ORDER = ("ENT", "MM", "SMB")


def _extract_quarter(horizon_quarter: str) -> str:
    """`FY2026-Q2` → `Q2`. Returns empty string if no Q segment found."""
    for part in horizon_quarter.split("-"):
        if part.startswith("Q") and part[1:].isdigit():
            return part
    return ""


class FunnelMetricsSheet(BaseSheet):
    sheet_name = "Funnel Metrics"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Funnel Metrics · {payload.horizon_quarter}",
            cols=len(_SEGMENT_HEADERS),
        )

        # --- stage distribution block ---
        H.write_header_row(ws, row=2, headers=list(_STAGE_HEADERS))
        stage_dist = aggregates.stage_distribution(payload.all_opps_snapshot)

        ordered_stages = [s for s in STAGES if s in stage_dist] + sorted(
            s for s in stage_dist if s not in STAGES
        )
        row = 3
        for stage in ordered_stages:
            bucket = stage_dist[stage]
            H.write_body_row(
                ws, row=row,
                values=(stage, bucket["count"], bucket["acv"], bucket["pct_of_pipeline"]),
                number_formats=list(_STAGE_FORMATS),
            )
            row += 1

        # --- segment close-rate block ---
        segment_block_row = row + 2
        H.write_header_row(ws, row=segment_block_row, headers=list(_SEGMENT_HEADERS))

        by_segment = aggregates.quarterly_closed_by_segment(payload.closed_opps_quarter)
        quarter = _extract_quarter(payload.horizon_quarter)

        row = segment_block_row + 1
        seen = set(_SEGMENT_ORDER)
        extras = sorted(s for s in by_segment if s not in seen)
        for seg in list(_SEGMENT_ORDER) + extras:
            bucket = by_segment.get(seg, {
                "won_count": 0, "won_acv": 0.0, "lost_count": 0, "lost_acv": 0.0,
            })
            won = int(bucket["won_count"])
            lost = int(bucket["lost_count"])
            closed_count = won + lost
            win_rate = (won / closed_count) if closed_count else 0.0
            target = quarterly_funnel_target(quarter, seg) if quarter else 0.0
            coverage = (closed_count / target) if target else 0.0
            H.write_body_row(
                ws, row=row,
                values=(
                    seg,
                    won, float(bucket["won_acv"]), lost, float(bucket["lost_acv"]),
                    win_rate,
                    target, closed_count, coverage,
                ),
                number_formats=list(_SEGMENT_FORMATS),
            )
            row += 1

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
