"""SDR Performance - SDR activity metrics (SDRs only from roster)."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


class SDRPerformanceSheet(BaseSheet):
    sheet_name = "SDR Performance"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg

        # Use SDR roster names only (not AEs who happen to create SDR-sourced opps)
        sdr_names = cfg.sdr_names

        # === SDR Summary ===
        self._write_section_title(ws, 1, "SDR Performance Summary")

        headers = [
            "SDR Name", "Role", "Segment", "Status",
            "Opps Created", "Pipeline Generated ($)",
            "Closed Won Attributed ($)", "Conversion Rate",
        ]
        self._write_headers(ws, 3, headers)

        sdr_opps = proc.sdr_opps_created
        sdr_pipe = proc.sdr_pipeline_generated
        sdr_won = proc.sdr_closed_won_attributed

        for i, sdr in enumerate(cfg.sdr_roster):
            row = 4 + i
            name = sdr["name"]
            opps = sdr_opps.get(name, 0)
            pipe = sdr_pipe.get(name, 0)
            won = sdr_won.get(name, 0)
            conv = won / pipe if pipe > 0 else 0

            self._write_cell(ws, row, 1, name, bold=True)
            self._write_cell(ws, row, 2, sdr.get("role", "SDR"))
            self._write_cell(ws, row, 3, sdr.get("segment", ""))
            self._write_cell(ws, row, 4, sdr.get("status", ""))
            self._write_cell(ws, row, 5, opps, fmt="number")
            self._write_cell(ws, row, 6, pipe, fmt="currency")
            self._write_cell(ws, row, 7, won, fmt="currency")
            self._write_cell(ws, row, 8, conv, fmt="percent")

        sdr_end = 3 + len(sdr_names)
        # Totals
        total_row = sdr_end + 1
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        self._write_cell(ws, total_row, 5,
                         sum(sdr_opps.get(n, 0) for n in sdr_names), fmt="number")
        self._write_cell(ws, total_row, 6,
                         sum(sdr_pipe.get(n, 0) for n in sdr_names), fmt="currency")
        self._write_cell(ws, total_row, 7,
                         sum(sdr_won.get(n, 0) for n in sdr_names), fmt="currency")

        # === Monthly Activity Grid ===
        grid_start = total_row + 3
        self._write_section_title(ws, grid_start, "Monthly Opps Created by SDR")

        grid_headers = ["SDR Name"] + MONTH_NAMES + ["Total"]
        self._write_headers(ws, grid_start + 2, grid_headers)

        sdr_monthly = proc.sdr_monthly_opps
        for i, sdr in enumerate(cfg.sdr_roster):
            row = grid_start + 3 + i
            name = sdr["name"]
            monthly = sdr_monthly.get(name, {})
            self._write_cell(ws, row, 1, name, bold=True)
            total = 0
            for j, m in enumerate(MONTHS):
                val = monthly.get(m, 0)
                total += val
                self._write_cell(ws, row, 2 + j, val, fmt="number")
            self._write_cell(ws, row, 14, total, fmt="number")

        # === Team-level metrics ===
        team_start = grid_start + 3 + len(sdr_names) + 3
        self._write_section_title(ws, team_start, "SDR Team Metrics")

        sdr_df = proc.df[proc.df["lead_source"] == "SDR"]
        total_sdr_opps = len(sdr_df)
        total_sdr_won = len(sdr_df[sdr_df["is_closed_won"]])
        total_sdr_rev = float(sdr_df[sdr_df["is_closed_won"]]["acv"].sum())

        self._write_cell(ws, team_start + 2, 1, "Total SDR-Sourced Opps", bold=True)
        self._write_cell(ws, team_start + 2, 2, total_sdr_opps, fmt="number")
        self._write_cell(ws, team_start + 3, 1, "SDR-Sourced Closed Won", bold=True)
        self._write_cell(ws, team_start + 3, 2, total_sdr_won, fmt="number")
        self._write_cell(ws, team_start + 4, 1, "SDR-Sourced Revenue", bold=True)
        self._write_cell(ws, team_start + 4, 2, total_sdr_rev, fmt="currency")
        self._write_cell(ws, team_start + 5, 1, "SDR Win Rate", bold=True)
        decided = len(sdr_df[sdr_df["stage"].isin(["Closed Won", "Closed Lost"])])
        self._write_cell(ws, team_start + 5, 2,
                         total_sdr_won / decided if decided > 0 else 0, fmt="percent")
        self._write_cell(ws, team_start + 6, 1, "Ramped SDRs", bold=True)
        self._write_cell(ws, team_start + 6, 2,
                         sum(1 for s in cfg.sdr_roster if s.get("status") == "ramped"), fmt="number")
        self._write_cell(ws, team_start + 7, 1, "Ramping SDRs", bold=True)
        self._write_cell(ws, team_start + 7, 2,
                         sum(1 for s in cfg.sdr_roster if s.get("status") == "ramping"), fmt="number")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=2)
        self._alt_row_shading(ws, 4, 4 + len(self.cfg.sdr_roster) - 1)
