"""Forecast sheet - Hybrid forecast with scenario analysis."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.charts import create_line_chart
from formatting.utils import auto_width, freeze_panes
from formatting.styles import FILL_SUBHEADER, FONT_SUBHEADER
from sheets.base import BaseSheet


class ForecastSheet(BaseSheet):
    sheet_name = "Forecast"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg
        forecast = proc.forecast

        # === Monthly Forecast ===
        self._write_section_title(ws, 1, "2026 Revenue Forecast (Hybrid Model)")

        headers = [
            "Month", "Method", "New Biz Forecast", "Expansion Forecast",
            "Total Forecast", "NB Target", "Exp Target", "Total Target",
        ]
        self._write_headers(ws, 3, headers)

        cum_forecast = 0
        cum_target = 0
        for i, m in enumerate(MONTHS):
            row = 4 + i
            f = forecast[m]
            nb_target = cfg.monthly_target(m, "new_biz")
            exp_target = cfg.monthly_target(m, "expansion")
            total_f = f["new_biz"] + f["expansion"]
            total_t = nb_target + exp_target
            cum_forecast += total_f
            cum_target += total_t

            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            self._write_cell(ws, row, 2, f["method"])
            self._write_cell(ws, row, 3, f["new_biz"], fmt="currency")
            self._write_cell(ws, row, 4, f["expansion"], fmt="currency")
            self._write_cell(ws, row, 5, total_f, fmt="currency")
            self._write_cell(ws, row, 6, nb_target, fmt="currency")
            self._write_cell(ws, row, 7, exp_target, fmt="currency")
            self._write_cell(ws, row, 8, total_t, fmt="currency")

        # Totals
        total_row = 16
        self._write_cell(ws, total_row, 1, "TOTAL", bold=True)
        self._write_cell(ws, total_row, 3,
                         sum(forecast[m]["new_biz"] for m in MONTHS), fmt="currency")
        self._write_cell(ws, total_row, 4,
                         sum(forecast[m]["expansion"] for m in MONTHS), fmt="currency")
        self._write_cell(ws, total_row, 5,
                         sum(forecast[m]["new_biz"] + forecast[m]["expansion"] for m in MONTHS),
                         fmt="currency")
        self._write_cell(ws, total_row, 6,
                         sum(cfg.monthly_target(m, "new_biz") for m in MONTHS), fmt="currency")
        self._write_cell(ws, total_row, 7,
                         sum(cfg.monthly_target(m, "expansion") for m in MONTHS), fmt="currency")
        self._write_cell(ws, total_row, 8,
                         cfg.targets["net_new_arr"] + cfg.targets["expansion_arr"], fmt="currency")

        # === Cumulative Forecast ===
        cum_start = 19
        self._write_section_title(ws, cum_start, "Cumulative Forecast vs Target")

        cum_headers = ["Month", "Cum. Forecast", "Cum. Target", "Gap"]
        self._write_headers(ws, cum_start + 2, cum_headers)

        cum_f = 0
        cum_t = 0
        for i, m in enumerate(MONTHS):
            row = cum_start + 3 + i
            f = forecast[m]
            cum_f += f["new_biz"] + f["expansion"]
            cum_t += cfg.monthly_target(m, "new_biz") + cfg.monthly_target(m, "expansion")

            self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
            self._write_cell(ws, row, 2, cum_f, fmt="currency")
            self._write_cell(ws, row, 3, cum_t, fmt="currency")
            self._write_cell(ws, row, 4, cum_f - cum_t, fmt="currency")

        # === Scenario Analysis ===
        scenario_start = cum_start + 17
        self._write_section_title(ws, scenario_start, "Scenario Analysis")

        sc_headers = ["Scenario", "Projected ARR", "vs Target", "Attainment %"]
        self._write_headers(ws, scenario_start + 2, sc_headers)

        annual_target = cfg.targets["net_new_arr"] + cfg.targets["expansion_arr"]
        scenarios = proc.forecast_scenarios
        for i, (name, value) in enumerate(scenarios.items()):
            row = scenario_start + 3 + i
            self._write_cell(ws, row, 1, name, bold=True)
            self._write_cell(ws, row, 2, value, fmt="currency")
            self._write_cell(ws, row, 3, value - annual_target, fmt="currency")
            self._write_cell(ws, row, 4, value / annual_target if annual_target else 0, fmt="percent")

        # === Methodology Note ===
        note_start = scenario_start + 8
        self._write_cell(ws, note_start, 1, "Forecast Methodology:", bold=True)
        self._write_cell(ws, note_start + 1, 1,
                         "- Past months: Actual closed won revenue")
        self._write_cell(ws, note_start + 2, 1,
                         "- Current + next month: Weighted pipeline (ACV x stage probability)")
        self._write_cell(ws, note_start + 3, 1,
                         "- Future months: Seasonally-adjusted run rate based on actuals to date")

        # Chart: Forecast vs Target trend
        cats = Reference(ws, min_col=1, min_row=4, max_row=15)
        data_ref = Reference(ws, min_col=5, min_row=3, max_row=15, max_col=8)
        chart = create_line_chart(
            ws, "Forecast vs Target (Monthly)", data_ref, cats,
            y_title="Revenue ($)",
            colors=["2E75B6", "E74C3C"],
        )
        ws.add_chart(chart, "J3")

        # === Rep Forecast vs SLT (appended when forecast data available) ===
        if proc.rep_forecast is not None:
            self._write_rep_comparison(ws, note_start + 6, proc, cfg)

    def _write_rep_comparison(self, ws, start_row, proc, cfg):
        """Append a Rep Forecast vs SLT summary table."""
        from core.forecast_loader import RepForecastData

        forecast_data: RepForecastData = proc.rep_forecast["data"]

        self._write_section_title(ws, start_row, "Rep Forecast vs SLT Summary")

        headers = ["Rep", "Manager", "SLT Forecast", "Rep Weighted", "Delta"]
        self._write_headers(ws, start_row + 2, headers)

        row = start_row + 3
        total_slt = 0
        total_rep = 0

        for name in cfg.ae_only_names:
            rf = forecast_data.reps.get(name)
            closed = proc.ae_closed_won.get(name, 0)
            slt = proc.ae_slt_forecast.get(name, 0)
            mgr = cfg.manager_for_ae(name)

            if rf:
                rep_w = closed + rf.commit_total * 0.9 + rf.hc_total * 0.75 + rf.longshot_total * 0.5
            else:
                rep_w = 0

            delta = slt - rep_w
            total_slt += slt
            total_rep += rep_w

            self._write_cell(ws, row, 1, name)
            self._write_cell(ws, row, 2, mgr)
            self._write_cell(ws, row, 3, slt, fmt="currency")
            self._write_cell(ws, row, 4, rep_w, fmt="currency")
            self._write_cell(ws, row, 5, delta, fmt="currency")
            row += 1

        # Total
        self._write_cell(ws, row, 1, "TOTAL", bold=True)
        self._write_cell(ws, row, 3, total_slt, fmt="currency")
        self._write_cell(ws, row, 4, total_rep, fmt="currency")
        self._write_cell(ws, row, 5, total_slt - total_rep, fmt="currency")

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=1)
