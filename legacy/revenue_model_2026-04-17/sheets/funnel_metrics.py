"""Funnel Metrics - Top-of-funnel conversion rates."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.charts import create_bar_chart
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet

# Stage order for funnel display (top to bottom)
FUNNEL_STAGES = [
    "New - Meeting Set", "No Show", "Demo", "Sent",
    "Pilot", "Business Case", "Proposal",
    "Closed Won", "Closed Lost", "Disqualified",
]


class FunnelMetricsSheet(BaseSheet):
    sheet_name = "Funnel Metrics"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc

        # === Stage Distribution ===
        self._write_section_title(ws, 1, "Opportunity Stage Distribution")

        headers = ["Stage", "Count", "% of Total"]
        self._write_headers(ws, 3, headers)

        total_opps = len(proc.df)
        funnel = proc.funnel_by_stage

        for i, stage in enumerate(FUNNEL_STAGES):
            row = 4 + i
            count = funnel.get(stage, 0)
            pct = count / total_opps if total_opps > 0 else 0
            self._write_cell(ws, row, 1, stage, bold=True)
            self._write_cell(ws, row, 2, count, fmt="number")
            self._write_cell(ws, row, 3, pct, fmt="percent")

        total_row = 4 + len(FUNNEL_STAGES)
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        self._write_cell(ws, total_row, 2, total_opps, fmt="number")

        # === Key Conversion Metrics ===
        metrics_start = total_row + 3
        self._write_section_title(ws, metrics_start, "Funnel Conversion Metrics")

        self._write_cell(ws, metrics_start + 2, 1, "Total Opportunities Created", bold=True)
        self._write_cell(ws, metrics_start + 2, 2, total_opps, fmt="number")

        self._write_cell(ws, metrics_start + 3, 1, "No-Show Count", bold=True)
        self._write_cell(ws, metrics_start + 3, 2, proc.no_show_count, fmt="number")

        self._write_cell(ws, metrics_start + 4, 1, "No-Show Rate", bold=True)
        self._write_cell(ws, metrics_start + 4, 2, proc.no_show_rate, fmt="percent")

        self._write_cell(ws, metrics_start + 5, 1, "Overall Win Rate", bold=True)
        self._write_cell(ws, metrics_start + 5, 2, proc.overall_win_rate, fmt="percent")

        demos = funnel.get("Demo", 0) + funnel.get("Pilot", 0) + funnel.get("Business Case", 0) + funnel.get("Proposal", 0) + funnel.get("Closed Won", 0)
        self._write_cell(ws, metrics_start + 6, 1, "Opps Reaching Demo+", bold=True)
        self._write_cell(ws, metrics_start + 6, 2, demos, fmt="number")

        proposals = funnel.get("Proposal", 0) + funnel.get("Closed Won", 0)
        self._write_cell(ws, metrics_start + 7, 1, "Opps Reaching Proposal+", bold=True)
        self._write_cell(ws, metrics_start + 7, 2, proposals, fmt="number")

        # === Monthly Opps Created ===
        monthly_start = metrics_start + 10
        self._write_section_title(ws, monthly_start, "Monthly Opportunities Created")

        m_headers = ["Month", "Opps Created", "Closed Won", "Win Count"]
        self._write_headers(ws, monthly_start + 2, m_headers)

        for i, m in enumerate(MONTHS):
            row = monthly_start + 3 + i
            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            self._write_cell(ws, row, 2, proc.monthly_opps_created[m], fmt="number")
            self._write_cell(ws, row, 3,
                             proc.monthly_closed_won_nb[m] + proc.monthly_expansion_actual[m],
                             fmt="currency")
            self._write_cell(ws, row, 4, proc.monthly_closed_won_count[m], fmt="number")

        # === Quarterly Funnel Targets vs Actuals (by Segment) ===
        funnel_start = monthly_start + 17
        self._write_section_title(ws, funnel_start, "Quarterly Funnel Targets vs Actuals (Deal Count)")

        ft_headers = [
            "Quarter", "Segment", "Deals Target", "Deals Actual TD", "Variance", "Attainment %",
        ]
        self._write_headers(ws, funnel_start + 2, ft_headers)

        cfg = self.cfg
        r = funnel_start + 3
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            for seg in ["SMB", "MM", "Ent"]:
                target = cfg.quarterly_funnel_target(q, seg)
                actual = proc.quarterly_funnel_actual.get(q, {}).get(seg, 0)
                variance = actual - target
                att = actual / target if target > 0 else 0
                self._write_cell(ws, r, 1, q, bold=True)
                self._write_cell(ws, r, 2, seg)
                self._write_cell(ws, r, 3, target, fmt="number")
                self._write_cell(ws, r, 4, actual, fmt="number")
                self._write_cell(ws, r, 5, variance, fmt="number")
                self._write_cell(ws, r, 6, att, fmt="percent")
                r += 1

        from formatting.conditional import add_attainment_formatting
        add_attainment_formatting(ws, f"F{funnel_start+3}:F{r-1}")

        # === Lead Source Analysis ===
        ls_start = r + 2
        self._write_section_title(ws, ls_start, "Lead Source Analysis")

        ls_headers = ["Lead Source", "Opps", "Won", "Revenue", "Win Rate"]
        self._write_headers(ws, ls_start + 2, ls_headers)

        sources = proc.funnel_by_lead_source
        sorted_sources = sorted(sources.items(), key=lambda x: x[1]["revenue"], reverse=True)

        for i, (src, data) in enumerate(sorted_sources):
            row = ls_start + 3 + i
            self._write_cell(ws, row, 1, src, bold=True)
            self._write_cell(ws, row, 2, data["total"], fmt="number")
            self._write_cell(ws, row, 3, data["won"], fmt="number")
            self._write_cell(ws, row, 4, data["revenue"], fmt="currency")
            self._write_cell(ws, row, 5, data["win_rate"], fmt="percent")

        # Chart
        stage_end = 3 + len(FUNNEL_STAGES)
        cats = Reference(ws, min_col=1, min_row=4, max_row=stage_end)
        data_ref = Reference(ws, min_col=2, min_row=3, max_row=stage_end)
        chart = create_bar_chart(
            ws, "Opportunities by Stage", data_ref, cats,
            y_title="Count", colors=["2E75B6"],
        )
        ws.add_chart(chart, "F3")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=1)
