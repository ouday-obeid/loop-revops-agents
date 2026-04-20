"""Rep Forecast sheet - Side-by-side SLT vs Rep forecast comparison."""

from __future__ import annotations

from openpyxl.chart import Reference
from openpyxl.worksheet.worksheet import Worksheet

from core.deal_matcher import MatchedDeal
from core.forecast_loader import RepForecastData
from formatting.charts import create_bar_chart
from formatting.conditional import add_variance_formatting
from formatting.styles import (
    BOTTOM_BORDER, FILL_SUBHEADER, FONT_BODY_BOLD, FONT_SUBHEADER,
)
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


# Tier weights for rep weighted forecast
COMMIT_WEIGHT = 0.90
HC_WEIGHT = 0.75
LONGSHOT_WEIGHT = 0.50


class RepForecastSheet(BaseSheet):
    sheet_name = "Rep Forecast"

    def _should_generate(self) -> bool:
        return self.proc.rep_forecast is not None

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg
        forecast_data: RepForecastData = proc.rep_forecast["data"]
        matched_deals: dict[str, list[MatchedDeal]] = proc.rep_forecast.get("matched_deals", {})

        # ================================================================
        # Section 1: Team Forecast Comparison
        # ================================================================
        self._write_section_title(ws, 1, "Team Forecast Comparison — SLT vs Rep")

        headers = [
            "Rep", "Manager", "Quota", "Closed Won",
            "SLT Forecast", "Rep Commit", "Rep HC", "Rep Longshot",
            "Rep Weighted", "Delta",
        ]
        self._write_headers(ws, 3, headers)

        # Build ordered list of reps grouped by manager
        manager_order = list(cfg.manager_groups.keys())
        rep_rows: list[dict] = []
        for mgr in manager_order:
            members = cfg.manager_groups[mgr]
            for name in members:
                if name not in cfg.ae_only_names:
                    continue
                rf = forecast_data.reps.get(name)
                closed = proc.ae_closed_won.get(name, 0)
                slt = proc.ae_slt_forecast.get(name, 0)
                quota = proc.ae_quota(name)

                if rf:
                    commit = rf.commit_total
                    hc = rf.hc_total
                    longshot = rf.longshot_total
                else:
                    commit = hc = longshot = 0.0

                rep_weighted = closed + commit * COMMIT_WEIGHT + hc * HC_WEIGHT + longshot * LONGSHOT_WEIGHT
                delta = slt - rep_weighted

                rep_rows.append({
                    "name": name,
                    "manager": mgr,
                    "quota": quota,
                    "closed": closed,
                    "slt": slt,
                    "commit": commit,
                    "hc": hc,
                    "longshot": longshot,
                    "rep_weighted": rep_weighted,
                    "delta": delta,
                })

        # Write data rows with manager subtotals
        row = 4
        current_mgr = None
        mgr_start_row = row
        section1_data_start = row

        for i, rd in enumerate(rep_rows):
            # Manager group separator
            if rd["manager"] != current_mgr:
                if current_mgr is not None:
                    # Write subtotal for previous manager
                    row = self._write_manager_subtotal(ws, row, current_mgr, rep_rows, mgr_start_row)
                    row += 1
                current_mgr = rd["manager"]
                mgr_start_row = row

            self._write_cell(ws, row, 1, rd["name"])
            self._write_cell(ws, row, 2, rd["manager"])
            self._write_cell(ws, row, 3, rd["quota"], fmt="currency")
            self._write_cell(ws, row, 4, rd["closed"], fmt="currency")
            self._write_cell(ws, row, 5, rd["slt"], fmt="currency")
            self._write_cell(ws, row, 6, rd["commit"], fmt="currency")
            self._write_cell(ws, row, 7, rd["hc"], fmt="currency")
            self._write_cell(ws, row, 8, rd["longshot"], fmt="currency")
            self._write_cell(ws, row, 9, rd["rep_weighted"], fmt="currency")
            self._write_cell(ws, row, 10, rd["delta"], fmt="currency")
            row += 1

        # Final manager subtotal
        if current_mgr is not None:
            row = self._write_manager_subtotal(ws, row, current_mgr, rep_rows, mgr_start_row)
            row += 1

        # Grand total
        total_row = row
        self._write_cell(ws, total_row, 1, "GRAND TOTAL", bold=True)
        for col, key in [(3, "quota"), (4, "closed"), (5, "slt"), (6, "commit"),
                         (7, "hc"), (8, "longshot"), (9, "rep_weighted"), (10, "delta")]:
            val = sum(rd[key] for rd in rep_rows)
            cell = self._write_cell(ws, total_row, col, val, fmt="currency")
            cell.font = FONT_BODY_BOLD
        for c in range(1, 11):
            ws.cell(row=total_row, column=c).border = BOTTOM_BORDER

        section1_end = total_row

        # Conditional formatting on Delta column
        add_variance_formatting(ws, f"J{section1_data_start}:J{section1_end}")

        # ================================================================
        # Section 2: Manager Summary
        # ================================================================
        sec2_start = section1_end + 3
        self._write_section_title(ws, sec2_start, "Manager Summary")

        mgr_headers = [
            "Manager", "Reps", "Total Quota", "Closed Won",
            "SLT Forecast", "Rep Weighted", "Delta", "Avg Attainment",
        ]
        self._write_headers(ws, sec2_start + 2, mgr_headers)

        mgr_row = sec2_start + 3
        for mgr in manager_order:
            mgr_reps = [rd for rd in rep_rows if rd["manager"] == mgr]
            if not mgr_reps:
                continue
            total_quota = sum(rd["quota"] for rd in mgr_reps)
            total_closed = sum(rd["closed"] for rd in mgr_reps)
            total_slt = sum(rd["slt"] for rd in mgr_reps)
            total_rep_w = sum(rd["rep_weighted"] for rd in mgr_reps)
            total_delta = sum(rd["delta"] for rd in mgr_reps)
            avg_att = total_closed / total_quota if total_quota > 0 else 0

            self._write_cell(ws, mgr_row, 1, mgr, bold=True)
            self._write_cell(ws, mgr_row, 2, len(mgr_reps), fmt="number")
            self._write_cell(ws, mgr_row, 3, total_quota, fmt="currency")
            self._write_cell(ws, mgr_row, 4, total_closed, fmt="currency")
            self._write_cell(ws, mgr_row, 5, total_slt, fmt="currency")
            self._write_cell(ws, mgr_row, 6, total_rep_w, fmt="currency")
            self._write_cell(ws, mgr_row, 7, total_delta, fmt="currency")
            self._write_cell(ws, mgr_row, 8, avg_att, fmt="percent")
            mgr_row += 1

        sec2_end = mgr_row - 1
        add_variance_formatting(ws, f"G{sec2_start + 3}:G{sec2_end}")

        # ================================================================
        # Section 3: Deal-Level Detail
        # ================================================================
        sec3_start = sec2_end + 3
        self._write_section_title(ws, sec3_start, "Deal-Level Detail (Reps with Forecast Tabs)")

        deal_headers = [
            "Rep", "Account", "Tier", "Forecast ACV",
            "SF Org", "SF Stage", "SF ACV", "ACV Delta", "Matched?",
            "Close Date", "Locations", "Products", "Contract Type", "Notes",
        ]
        self._write_headers(ws, sec3_start + 2, deal_headers)

        deal_row = sec3_start + 3
        has_deals = False

        for rep_name in cfg.ae_only_names:
            rf = forecast_data.reps.get(rep_name)
            if not rf or not rf.has_detail_tab:
                continue

            rep_matched = matched_deals.get(rep_name, [])
            if not rep_matched and rf.deals:
                # No matching was done, show deals without SF match
                rep_matched = [
                    MatchedDeal(forecast_deal=d) for d in rf.deals
                ]

            for md in rep_matched:
                # Skip "New - Meeting Set" deals from forecast detail
                if md.matched and md.sf_stage == "New - Meeting Set":
                    continue
                has_deals = True
                d = md.forecast_deal
                self._write_cell(ws, deal_row, 1, rep_name)
                self._write_cell(ws, deal_row, 2, d.name)
                self._write_cell(ws, deal_row, 3, d.tier)
                self._write_cell(ws, deal_row, 4, d.acv, fmt="currency")
                self._write_cell(ws, deal_row, 5, md.sf_org if md.matched else "")
                self._write_cell(ws, deal_row, 6, md.sf_stage if md.matched else "")
                self._write_cell(ws, deal_row, 7, md.sf_acv if md.matched else 0, fmt="currency")
                self._write_cell(ws, deal_row, 8, md.acv_delta if md.matched else 0, fmt="currency")
                self._write_cell(ws, deal_row, 9, "Yes" if md.matched else "No")
                self._write_cell(ws, deal_row, 10, md.sf_close_date if md.matched else "")
                self._write_cell(ws, deal_row, 11, md.sf_locations if md.matched else 0, fmt="number")
                self._write_cell(ws, deal_row, 12, md.sf_products if md.matched else "")
                self._write_cell(ws, deal_row, 13, md.sf_contract_type if md.matched else "")
                self._write_cell(ws, deal_row, 14, md.sf_opp_notes if md.matched else "")
                deal_row += 1

        if not has_deals:
            self._write_cell(ws, deal_row, 1, "(No reps with individual forecast tabs)")
            deal_row += 1

        sec3_end = deal_row - 1

        # ================================================================
        # Section 4: Scenario Comparison
        # ================================================================
        sec4_start = sec3_end + 3
        self._write_section_title(ws, sec4_start, "Scenario Comparison — SLT vs Rep")

        sc_headers = ["Scenario", "SLT Projection", "Rep Weighted", "Blended"]
        self._write_headers(ws, sec4_start + 2, sc_headers)

        # Compute totals
        total_slt = sum(rd["slt"] for rd in rep_rows)
        total_rep_w = sum(rd["rep_weighted"] for rd in rep_rows)

        scenarios = [
            ("Conservative", 0.85),
            ("Base", 1.0),
            ("Optimistic", 1.15),
        ]

        sc_row = sec4_start + 3
        for name, multiplier in scenarios:
            slt_val = total_slt * multiplier
            rep_val = total_rep_w * multiplier
            blended = (slt_val + rep_val) / 2

            self._write_cell(ws, sc_row, 1, name, bold=True)
            self._write_cell(ws, sc_row, 2, slt_val, fmt="currency")
            self._write_cell(ws, sc_row, 3, rep_val, fmt="currency")
            self._write_cell(ws, sc_row, 4, blended, fmt="currency")
            sc_row += 1

        sec4_end = sc_row - 1

        # ================================================================
        # Chart: Clustered bar — SLT vs Rep by Manager
        # ================================================================
        chart_start = sec4_end + 3
        self._write_section_title(ws, chart_start, "SLT vs Rep Forecast by Manager")

        # Write chart data table
        chart_headers = ["Manager", "SLT Forecast", "Rep Weighted"]
        self._write_headers(ws, chart_start + 2, chart_headers)

        chart_row = chart_start + 3
        for mgr in manager_order:
            mgr_reps = [rd for rd in rep_rows if rd["manager"] == mgr]
            if not mgr_reps:
                continue
            self._write_cell(ws, chart_row, 1, mgr)
            self._write_cell(ws, chart_row, 2, sum(rd["slt"] for rd in mgr_reps), fmt="currency")
            self._write_cell(ws, chart_row, 3, sum(rd["rep_weighted"] for rd in mgr_reps), fmt="currency")
            chart_row += 1

        chart_data_end = chart_row - 1

        if chart_data_end >= chart_start + 3:
            cats = Reference(ws, min_col=1, min_row=chart_start + 3, max_row=chart_data_end)
            data_ref = Reference(ws, min_col=2, min_row=chart_start + 2, max_row=chart_data_end, max_col=3)
            chart = create_bar_chart(
                ws, "SLT vs Rep Forecast by Manager",
                data_ref, cats,
                y_title="Forecast ($)",
                colors=["2E75B6", "27AE60"],
            )
            ws.add_chart(chart, f"E{chart_start + 2}")

    def _write_manager_subtotal(self, ws, row, manager, rep_rows, mgr_start_row) -> int:
        """Write a subtotal row for a manager group. Returns the next row."""
        mgr_reps = [rd for rd in rep_rows if rd["manager"] == manager]
        self._write_cell(ws, row, 1, f"{manager} Subtotal", bold=True)
        for col, key in [(3, "quota"), (4, "closed"), (5, "slt"), (6, "commit"),
                         (7, "hc"), (8, "longshot"), (9, "rep_weighted"), (10, "delta")]:
            val = sum(rd[key] for rd in mgr_reps)
            cell = self._write_cell(ws, row, col, val, fmt="currency")
            cell.font = FONT_BODY_BOLD
        # Add subtle border
        for c in range(1, 11):
            cell = ws.cell(row=row, column=c)
            cell.fill = FILL_SUBHEADER
        return row + 1

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=4, col=2)
