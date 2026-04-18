"""AE Scorecard - Per-AE performance metrics (AEs only, no SDRs/Managers)."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.conditional import add_attainment_formatting, add_data_bars
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


class AEScorecardSheet(BaseSheet):
    sheet_name = "AE Scorecard"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg

        # Only actual AEs (filter out Managers, SDRs, etc.)
        ae_roster = cfg.ae_only_roster

        # Current quarter info
        from datetime import datetime
        now = datetime.now()
        q_num = (now.month - 1) // 3 + 1
        q_label = f"Q{q_num}"
        q_months = list(range((q_num - 1) * 3 + 1, q_num * 3 + 1))

        # === Summary Table ===
        self._write_section_title(ws, 1, "AE Performance Summary")

        headers = [
            "AE Name", "Segment", "Status", "Quota",
            f"{q_label} Closed", f"{q_label} Attain %",
            "YTD Closed", "YTD Attain %",
            "Pipeline ($)", "Weighted Pipeline",
            "Coverage Ratio", "Deals Won", "Open Deals",
        ]
        self._write_headers(ws, 3, headers)

        for i, ae in enumerate(ae_roster):
            row = 4 + i
            name = ae["name"]
            quota = float(ae.get("quota", 0))
            q_quota = quota / 4  # quarterly quota

            # QTD closed
            ae_won = proc.closed_won[proc.closed_won["owner"] == name]
            qtd_closed = float(ae_won[ae_won["close_month"].isin(q_months)]["acv"].sum())
            qtd_attainment = qtd_closed / q_quota if q_quota > 0 else 0

            # YTD closed
            ytd_closed = proc.ae_closed_won.get(name, 0)
            ytd_attainment = ytd_closed / quota if quota > 0 else 0

            pipeline = proc.ae_pipeline.get(name, 0)
            weighted = proc.ae_weighted_pipeline.get(name, 0)
            remaining = quota - ytd_closed
            coverage = pipeline / remaining if remaining > 0 else 0
            won_count = proc.ae_closed_won_count.get(name, 0)
            open_count = proc.ae_deal_count.get(name, 0)

            self._write_cell(ws, row, 1, name, bold=True)
            self._write_cell(ws, row, 2, ae.get("segment", ""))
            self._write_cell(ws, row, 3, ae.get("status", ""))
            self._write_cell(ws, row, 4, quota, fmt="currency")
            self._write_cell(ws, row, 5, qtd_closed, fmt="currency")
            self._write_cell(ws, row, 6, qtd_attainment, fmt="percent")
            self._write_cell(ws, row, 7, ytd_closed, fmt="currency")
            self._write_cell(ws, row, 8, ytd_attainment, fmt="percent")
            self._write_cell(ws, row, 9, pipeline, fmt="currency")
            self._write_cell(ws, row, 10, weighted, fmt="currency")
            self._write_cell(ws, row, 11, coverage, fmt="number")
            self._write_cell(ws, row, 12, won_count, fmt="number")
            self._write_cell(ws, row, 13, open_count, fmt="number")

        last_ae_row = 3 + len(ae_roster)

        # Totals
        total_row = last_ae_row + 1
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        self._write_cell(ws, total_row, 4,
                         sum(ae.get("quota", 0) for ae in ae_roster), fmt="currency")
        total_qtd = sum(
            float(proc.closed_won[
                (proc.closed_won["owner"] == ae["name"]) &
                (proc.closed_won["close_month"].isin(q_months))
            ]["acv"].sum()) for ae in ae_roster
        )
        self._write_cell(ws, total_row, 5, total_qtd, fmt="currency")
        self._write_cell(ws, total_row, 7,
                         sum(proc.ae_closed_won.get(ae["name"], 0) for ae in ae_roster),
                         fmt="currency")
        self._write_cell(ws, total_row, 9,
                         sum(proc.ae_pipeline.get(ae["name"], 0) for ae in ae_roster),
                         fmt="currency")
        self._write_cell(ws, total_row, 10,
                         sum(proc.ae_closed_won_count.get(ae["name"], 0) for ae in ae_roster),
                         fmt="number")

        # === Monthly Breakdown Grid ===
        grid_start = total_row + 3
        self._write_section_title(ws, grid_start, "Monthly Closed Won by AE ($)")

        grid_headers = ["AE Name"] + MONTH_NAMES + ["Total"]
        self._write_headers(ws, grid_start + 2, grid_headers)

        for i, ae in enumerate(ae_roster):
            row = grid_start + 3 + i
            name = ae["name"]
            monthly = proc.ae_monthly_closed(name)
            self._write_cell(ws, row, 1, name, bold=True)
            for j, m in enumerate(MONTHS):
                self._write_cell(ws, row, 2 + j, monthly[m], fmt="currency")
            self._write_cell(ws, row, 14, sum(monthly.values()), fmt="currency")

        # Conditional formatting
        add_attainment_formatting(ws, f"F4:F{last_ae_row}")
        add_attainment_formatting(ws, f"H4:H{last_ae_row}")
        add_data_bars(ws, f"K4:K{last_ae_row}")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=2)
        self._alt_row_shading(ws, 4, 4 + len(self.cfg.ae_only_roster) - 1)
