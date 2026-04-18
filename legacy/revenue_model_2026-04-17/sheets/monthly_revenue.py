"""Monthly Revenue sheet - Target vs Actual by month."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.charts import create_combo_chart
from formatting.conditional import add_attainment_formatting, add_variance_formatting
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


class MonthlyRevenueSheet(BaseSheet):
    sheet_name = "Monthly Revenue"

    def _write(self, ws: Worksheet) -> None:
        cfg = self.cfg
        proc = self.proc

        # === New Business Section ===
        self._write_section_title(ws, 1, "New Business Revenue - Monthly Tracking")

        headers = ["Month", "Target", "Actual", "Variance", "Attainment %",
                    "Cumulative Target", "Cumulative Actual", "Cumulative Att.%"]
        self._write_headers(ws, 3, headers)

        cum_target = 0
        cum_actual = 0
        for i, m in enumerate(MONTHS):
            row = 4 + i
            target = cfg.monthly_target(m, "new_biz")
            actual = proc.monthly_closed_won_nb[m]
            variance = actual - target
            attainment = actual / target if target > 0 else 0
            cum_target += target
            cum_actual += actual
            cum_att = cum_actual / cum_target if cum_target > 0 else 0

            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            self._write_cell(ws, row, 2, target, fmt="currency")
            self._write_cell(ws, row, 3, actual, fmt="currency")
            self._write_cell(ws, row, 4, variance, fmt="currency")
            self._write_cell(ws, row, 5, attainment, fmt="percent")
            self._write_cell(ws, row, 6, cum_target, fmt="currency")
            self._write_cell(ws, row, 7, cum_actual, fmt="currency")
            self._write_cell(ws, row, 8, cum_att, fmt="percent")

        # Totals row
        total_row = 16
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        total_target = sum(cfg.monthly_target(m, "new_biz") for m in MONTHS)
        total_actual = sum(proc.monthly_closed_won_nb[m] for m in MONTHS)
        self._write_cell(ws, total_row, 2, total_target, fmt="currency")
        self._write_cell(ws, total_row, 3, total_actual, fmt="currency")
        self._write_cell(ws, total_row, 4, total_actual - total_target, fmt="currency")
        self._write_cell(ws, total_row, 5,
                         total_actual / total_target if total_target else 0, fmt="percent")

        # === Expansion Section ===
        exp_start = 19
        self._write_section_title(ws, exp_start, "Expansion Revenue - Monthly Tracking")
        self._write_headers(ws, exp_start + 2, headers)

        cum_target = 0
        cum_actual = 0
        for i, m in enumerate(MONTHS):
            row = exp_start + 3 + i
            target = cfg.monthly_target(m, "expansion")
            actual = proc.monthly_expansion_actual[m]
            variance = actual - target
            attainment = actual / target if target > 0 else 0
            cum_target += target
            cum_actual += actual
            cum_att = cum_actual / cum_target if cum_target > 0 else 0

            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            self._write_cell(ws, row, 2, target, fmt="currency")
            self._write_cell(ws, row, 3, actual, fmt="currency")
            self._write_cell(ws, row, 4, variance, fmt="currency")
            self._write_cell(ws, row, 5, attainment, fmt="percent")
            self._write_cell(ws, row, 6, cum_target, fmt="currency")
            self._write_cell(ws, row, 7, cum_actual, fmt="currency")
            self._write_cell(ws, row, 8, cum_att, fmt="percent")

        # Chart: NB Target vs Actual
        cats = Reference(ws, min_col=1, min_row=4, max_row=15)
        bar_ref = Reference(ws, min_col=2, min_row=3, max_row=15)
        line_ref = Reference(ws, min_col=3, min_row=3, max_row=15)
        chart = create_combo_chart(
            ws, "New Business: Target vs Actual",
            bar_ref, line_ref, cats,
            width=20, height=12,
        )
        ws.add_chart(chart, "J3")

        # Conditional formatting
        add_attainment_formatting(ws, f"E4:E15")
        add_attainment_formatting(ws, f"H4:H15")
        add_variance_formatting(ws, f"D4:D15")
        add_attainment_formatting(ws, f"E{exp_start+3}:E{exp_start+14}")
        add_variance_formatting(ws, f"D{exp_start+3}:D{exp_start+14}")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=1)
