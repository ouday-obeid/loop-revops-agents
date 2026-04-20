"""Pipeline Analysis - Pipeline by stage, segment, aging."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from formatting.charts import create_bar_chart
from formatting.conditional import add_data_bars
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


class PipelineAnalysisSheet(BaseSheet):
    sheet_name = "Pipeline Analysis"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc

        # === Pipeline by Stage ===
        self._write_section_title(ws, 1, "Pipeline by Stage")

        headers = ["Stage", "Phase", "Deal Count", "Total ACV", "Weighted ACV", "Avg Deal Size"]
        self._write_headers(ws, 3, headers)

        stages = proc.pipeline_by_stage
        for i, s in enumerate(stages):
            row = 4 + i
            avg = s["acv"] / s["count"] if s["count"] > 0 else 0
            self._write_cell(ws, row, 1, s["stage"], bold=True)
            self._write_cell(ws, row, 2, s["phase"])
            self._write_cell(ws, row, 3, s["count"], fmt="number")
            self._write_cell(ws, row, 4, s["acv"], fmt="currency")
            self._write_cell(ws, row, 5, s["weighted_acv"], fmt="currency")
            self._write_cell(ws, row, 6, avg, fmt="currency")

        stage_end = 3 + len(stages)
        # Totals
        total_row = stage_end + 1
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        self._write_cell(ws, total_row, 3,
                         sum(s["count"] for s in stages), fmt="number")
        self._write_cell(ws, total_row, 4,
                         sum(s["acv"] for s in stages), fmt="currency")
        self._write_cell(ws, total_row, 5,
                         sum(s["weighted_acv"] for s in stages), fmt="currency")

        # === Pipeline by Segment ===
        seg_start = total_row + 3
        self._write_section_title(ws, seg_start, "Pipeline by Segment")

        seg_headers = ["Segment", "Deal Count", "Total ACV", "Weighted ACV", "% of Pipeline"]
        self._write_headers(ws, seg_start + 2, seg_headers)

        total_pipe = proc.total_pipeline_acv
        for i, seg in enumerate(["SMB", "MM", "Ent"]):
            row = seg_start + 3 + i
            data = proc.pipeline_by_segment[seg]
            pct = data["acv"] / total_pipe if total_pipe > 0 else 0
            self._write_cell(ws, row, 1, seg, bold=True)
            self._write_cell(ws, row, 2, data["count"], fmt="number")
            self._write_cell(ws, row, 3, data["acv"], fmt="currency")
            self._write_cell(ws, row, 4, data["weighted_acv"], fmt="currency")
            self._write_cell(ws, row, 5, pct, fmt="percent")

        # === Aging Analysis ===
        aging_start = seg_start + 8
        self._write_section_title(ws, aging_start, "Pipeline Aging Analysis")

        aging_headers = ["Aging Bucket", "Deal Count", "Total ACV", "% of Pipeline"]
        self._write_headers(ws, aging_start + 2, aging_headers)

        for i, bucket in enumerate(["0-30", "31-60", "61-90", "90+"]):
            row = aging_start + 3 + i
            data = proc.pipeline_by_aging[bucket]
            pct = data["acv"] / total_pipe if total_pipe > 0 else 0
            self._write_cell(ws, row, 1, bucket, bold=True)
            self._write_cell(ws, row, 2, data["count"], fmt="number")
            self._write_cell(ws, row, 3, data["acv"], fmt="currency")
            self._write_cell(ws, row, 4, pct, fmt="percent")

        # === Key Metrics ===
        metrics_start = aging_start + 9
        self._write_section_title(ws, metrics_start, "Pipeline Health Metrics")

        self._write_cell(ws, metrics_start + 2, 1, "Total Pipeline ACV", bold=True)
        self._write_cell(ws, metrics_start + 2, 2, proc.total_pipeline_acv, fmt="currency")
        self._write_cell(ws, metrics_start + 3, 1, "Weighted Pipeline", bold=True)
        self._write_cell(ws, metrics_start + 3, 2, proc.total_weighted_pipeline, fmt="currency")
        self._write_cell(ws, metrics_start + 4, 1, "Pipeline Coverage", bold=True)
        self._write_cell(ws, metrics_start + 4, 2, proc.pipeline_coverage, fmt="number")
        self._write_cell(ws, metrics_start + 5, 1, "Overall Win Rate", bold=True)
        self._write_cell(ws, metrics_start + 5, 2, proc.overall_win_rate, fmt="percent")
        self._write_cell(ws, metrics_start + 6, 1, "Avg Deal Size", bold=True)
        self._write_cell(ws, metrics_start + 6, 2, proc.avg_deal_size, fmt="currency")
        self._write_cell(ws, metrics_start + 7, 1, "Avg Sales Cycle (days)", bold=True)
        self._write_cell(ws, metrics_start + 7, 2, proc.avg_sales_cycle, fmt="number")

        # Chart: Pipeline by stage
        if stages:
            cats = Reference(ws, min_col=1, min_row=4, max_row=stage_end)
            data_ref = Reference(ws, min_col=4, min_row=3, max_row=stage_end)
            chart = create_bar_chart(
                ws, "Pipeline ACV by Stage", data_ref, cats,
                y_title="ACV ($)", colors=["2E75B6"],
            )
            ws.add_chart(chart, "H3")

        add_data_bars(ws, f"D4:D{stage_end}")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=1)
