"""Expansion Revenue Tracking sheet."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.charts import create_combo_chart
from formatting.conditional import add_attainment_formatting, add_variance_formatting
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


class ExpansionSheet(BaseSheet):
    sheet_name = "Expansion"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg

        # === Monthly Target vs Actual ===
        self._write_section_title(ws, 1, "Expansion Revenue - Monthly Tracking")

        headers = ["Month", "Target", "Actual", "Variance", "Attainment %"]
        self._write_headers(ws, 3, headers)

        for i, m in enumerate(MONTHS):
            row = 4 + i
            target = cfg.monthly_target(m, "expansion")
            actual = proc.monthly_expansion_actual[m]
            variance = actual - target
            att = actual / target if target > 0 else 0

            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            self._write_cell(ws, row, 2, target, fmt="currency")
            self._write_cell(ws, row, 3, actual, fmt="currency")
            self._write_cell(ws, row, 4, variance, fmt="currency")
            self._write_cell(ws, row, 5, att, fmt="percent")

        total_row = 16
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        total_target = sum(cfg.monthly_target(m, "expansion") for m in MONTHS)
        total_actual = sum(proc.monthly_expansion_actual[m] for m in MONTHS)
        self._write_cell(ws, total_row, 2, total_target, fmt="currency")
        self._write_cell(ws, total_row, 3, total_actual, fmt="currency")
        self._write_cell(ws, total_row, 4, total_actual - total_target, fmt="currency")
        self._write_cell(ws, total_row, 5,
                         total_actual / total_target if total_target else 0, fmt="percent")

        # === By AE ===
        ae_start = 19
        self._write_section_title(ws, ae_start, "Expansion by AE")

        ae_headers = ["AE Name", "Expansion Revenue", "Deal Count"]
        self._write_headers(ws, ae_start + 2, ae_headers)

        exp_by_ae = proc.expansion_by_ae
        sorted_aes = sorted(exp_by_ae.items(), key=lambda x: x[1], reverse=True)

        for i, (name, rev) in enumerate(sorted_aes):
            row = ae_start + 3 + i
            count = len(proc.closed_won_exp[proc.closed_won_exp["owner"] == name])
            self._write_cell(ws, row, 1, name, bold=True)
            self._write_cell(ws, row, 2, rev, fmt="currency")
            self._write_cell(ws, row, 3, count, fmt="number")

        # === Deal Detail ===
        detail_start = ae_start + 3 + len(sorted_aes) + 3
        self._write_section_title(ws, detail_start, "Expansion Deal Detail")

        detail_headers = ["Organization", "AE", "Opportunity", "ACV", "Close Date"]
        self._write_headers(ws, detail_start + 2, detail_headers)

        detail_df = proc.expansion_by_account
        for i, (_, deal) in enumerate(detail_df.iterrows()):
            row = detail_start + 3 + i
            self._write_cell(ws, row, 1, deal["organization"])
            self._write_cell(ws, row, 2, deal["owner"])
            self._write_cell(ws, row, 3, deal["opp_name"])
            self._write_cell(ws, row, 4, deal["acv"], fmt="currency")
            cd = deal["close_date"]
            self._write_cell(ws, row, 5, cd.strftime("%m/%d/%Y") if hasattr(cd, "strftime") else str(cd))

        # Chart
        cats = Reference(ws, min_col=1, min_row=4, max_row=15)
        bar_ref = Reference(ws, min_col=2, min_row=3, max_row=15)
        line_ref = Reference(ws, min_col=3, min_row=3, max_row=15)
        chart = create_combo_chart(
            ws, "Expansion: Target vs Actual",
            bar_ref, line_ref, cats,
        )
        ws.add_chart(chart, "G3")

        # Conditional formatting
        add_attainment_formatting(ws, f"E4:E15")
        add_variance_formatting(ws, f"D4:D15")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=1)
