"""Segment Analysis - SMB / MM / Ent deep dive with ARPL, ADS, LPD."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.charts import create_pie_chart
from formatting.conditional import add_variance_formatting
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet

SEG_ORDER = ["SMB", "MM", "Ent"]


class SegmentAnalysisSheet(BaseSheet):
    sheet_name = "Segment Analysis"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg

        # === Segment Performance Summary ===
        self._write_section_title(ws, 1, "Segment Performance Summary")

        headers = [
            "Segment", "Deals Won", "Revenue", "Win Rate",
            "Actual ADS", "Target ADS", "ADS Variance",
            "Pipeline Deals", "Pipeline ACV",
        ]
        self._write_headers(ws, 3, headers)

        for i, seg in enumerate(SEG_ORDER):
            row = 4 + i
            data = proc.segment_summary[seg]
            target_ads = cfg.segment_target_ads(seg)
            ads_var = data["avg_deal_size"] - target_ads

            self._write_cell(ws, row, 1, seg, bold=True)
            self._write_cell(ws, row, 2, data["deals_won"], fmt="number")
            self._write_cell(ws, row, 3, data["revenue"], fmt="currency")
            self._write_cell(ws, row, 4, data["win_rate"], fmt="percent")
            self._write_cell(ws, row, 5, data["avg_deal_size"], fmt="currency")
            self._write_cell(ws, row, 6, target_ads, fmt="currency")
            self._write_cell(ws, row, 7, ads_var, fmt="currency")
            self._write_cell(ws, row, 8, data["pipeline_count"], fmt="number")
            self._write_cell(ws, row, 9, data["pipeline_acv"], fmt="currency")

        # Totals
        self._write_cell(ws, 7, 1, "TOTAL", bold=True)
        self._write_cell(ws, 7, 2,
                         sum(proc.segment_summary[s]["deals_won"] for s in SEG_ORDER), fmt="number")
        self._write_cell(ws, 7, 3,
                         sum(proc.segment_summary[s]["revenue"] for s in SEG_ORDER), fmt="currency")

        add_variance_formatting(ws, "G4:G6")

        # === ARPL: Target vs Current ===
        arpl_start = 10
        self._write_section_title(ws, arpl_start, "ARPL (ARR Per Location) - Target vs Current")

        arpl_headers = ["Segment", "ARPL Target", "ARPL Current", "Variance"]
        self._write_headers(ws, arpl_start + 2, arpl_headers)

        for i, seg in enumerate(SEG_ORDER):
            row = arpl_start + 3 + i
            target = cfg.segment_target_arpl(seg)
            current = proc.current_arpl.get(seg, 0)
            self._write_cell(ws, row, 1, seg, bold=True)
            self._write_cell(ws, row, 2, target, fmt="currency")
            self._write_cell(ws, row, 3, current, fmt="currency")
            self._write_cell(ws, row, 4, current - target, fmt="currency")

        # Blended ARPL
        blend_row = arpl_start + 6
        blended_target = cfg.blended_targets.get("arpl", 0)
        blended_current = proc.current_arpl.get("Blended", 0)
        self._write_cell(ws, blend_row, 1, "Blended", bold=True)
        self._write_cell(ws, blend_row, 2, blended_target, fmt="currency")
        self._write_cell(ws, blend_row, 3, blended_current, fmt="currency")
        self._write_cell(ws, blend_row, 4, blended_current - blended_target, fmt="currency")

        add_variance_formatting(ws, f"D{arpl_start+3}:D{blend_row}")

        # === ADS: Target vs Current ===
        ads_start = arpl_start + 9
        self._write_section_title(ws, ads_start, "ADS (Average Deal Size) - Target vs Current")

        ads_headers = ["Segment", "ADS Target", "ADS Current", "Variance"]
        self._write_headers(ws, ads_start + 2, ads_headers)

        for i, seg in enumerate(SEG_ORDER):
            row = ads_start + 3 + i
            target = cfg.segment_target_ads(seg)
            current = proc.current_ads.get(seg, 0)
            self._write_cell(ws, row, 1, seg, bold=True)
            self._write_cell(ws, row, 2, target, fmt="currency")
            self._write_cell(ws, row, 3, current, fmt="currency")
            self._write_cell(ws, row, 4, current - target, fmt="currency")

        # Blended ADS
        ads_blend = ads_start + 6
        ads_blended_target = cfg.blended_targets.get("ads", 0)
        ads_blended_current = proc.current_ads.get("Blended", 0)
        self._write_cell(ws, ads_blend, 1, "Blended", bold=True)
        self._write_cell(ws, ads_blend, 2, ads_blended_target, fmt="currency")
        self._write_cell(ws, ads_blend, 3, ads_blended_current, fmt="currency")
        self._write_cell(ws, ads_blend, 4, ads_blended_current - ads_blended_target, fmt="currency")

        add_variance_formatting(ws, f"D{ads_start+3}:D{ads_blend}")

        # === Avg Locations Per Deal: Target vs Current ===
        lpd_start = ads_start + 9
        self._write_section_title(ws, lpd_start, "Avg Locations Per Deal - Target vs Current")

        lpd_headers = ["Segment", "LPD Target", "LPD Current", "Variance"]
        self._write_headers(ws, lpd_start + 2, lpd_headers)

        for i, seg in enumerate(SEG_ORDER):
            row = lpd_start + 3 + i
            target = cfg.segment_target_lpd(seg)
            current = proc.current_lpd.get(seg, 0)
            self._write_cell(ws, row, 1, seg, bold=True)
            self._write_cell(ws, row, 2, target, fmt="number")
            self._write_cell(ws, row, 3, current, fmt="number")
            self._write_cell(ws, row, 4, current - target, fmt="number")

        # Blended LPD
        lpd_blend = lpd_start + 6
        lpd_blended_target = cfg.blended_targets.get("lpd", 0)
        lpd_blended_current = proc.current_lpd.get("Blended", 0)
        self._write_cell(ws, lpd_blend, 1, "Blended", bold=True)
        self._write_cell(ws, lpd_blend, 2, lpd_blended_target, fmt="number")
        self._write_cell(ws, lpd_blend, 3, lpd_blended_current, fmt="number")
        self._write_cell(ws, lpd_blend, 4, lpd_blended_current - lpd_blended_target, fmt="number")

        # === Monthly Segment Trend ===
        trend_start = lpd_start + 9
        self._write_section_title(ws, trend_start, "Monthly Revenue by Segment")

        trend_headers = ["Month"] + SEG_ORDER + ["Total"]
        self._write_headers(ws, trend_start + 2, trend_headers)

        for i, m in enumerate(MONTHS):
            row = trend_start + 3 + i
            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            month_total = 0
            for j, seg in enumerate(SEG_ORDER):
                seg_won = proc.closed_won[
                    (proc.closed_won["segment"] == seg) &
                    (proc.closed_won["close_month"] == m)
                ]
                val = float(seg_won["acv"].sum())
                month_total += val
                self._write_cell(ws, row, 2 + j, val, fmt="currency")
            self._write_cell(ws, row, 5, month_total, fmt="currency")

        # === Revenue Mix Pie Chart ===
        pie_start = trend_start + 17
        self._write_cell(ws, pie_start, 1, "Segment", bold=True)
        self._write_cell(ws, pie_start, 2, "Revenue", bold=True)
        for i, seg in enumerate(SEG_ORDER):
            self._write_cell(ws, pie_start + 1 + i, 1, seg)
            self._write_cell(ws, pie_start + 1 + i, 2,
                             proc.segment_summary[seg]["revenue"], fmt="currency")

        cats = Reference(ws, min_col=1, min_row=pie_start + 1, max_row=pie_start + 3)
        data_ref = Reference(ws, min_col=2, min_row=pie_start, max_row=pie_start + 3)
        chart = create_pie_chart(
            ws, "Revenue Mix by Segment", data_ref, cats,
            colors=["2E75B6", "27AE60", "F39C12"],
        )
        ws.add_chart(chart, "G3")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=1)
