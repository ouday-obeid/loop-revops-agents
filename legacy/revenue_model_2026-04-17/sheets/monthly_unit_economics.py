"""Monthly Unit Economics sheet — ARPL, ADS, Avg Locs/Deal by segment per month."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from core.processor import MONTH_NAMES, MONTHS
from formatting.utils import auto_width, freeze_panes
from sheets.base import BaseSheet


class MonthlyUnitEconomicsSheet(BaseSheet):
    sheet_name = "Monthly Unit Economics"

    def _write(self, ws: Worksheet) -> None:
        proc = self.proc
        cfg = self.cfg
        segs = ["SMB", "MM", "Ent"]

        metrics = [
            ("ARPL", "ARPL", "currency"),
            ("ADS", "ADS", "currency"),
            ("Avg Locs/Deal", "LPD", "number"),
        ]

        current_row = 1

        for metric_label, metric_key, fmt in metrics:
            # Section title
            self._write_section_title(ws, current_row, f"{metric_label} — Target vs Current by Month")
            current_row += 1

            # Headers
            headers = ["Month"]
            for seg in segs:
                headers.extend([f"{seg} Target", f"{seg} Current"])
            headers.extend(["Blended Target", "Blended Current"])
            self._write_headers(ws, current_row, headers)
            current_row += 1

            # Target lookups
            seg_targets = {}
            blended_target = 0
            for seg in segs:
                if metric_key == "ARPL":
                    seg_targets[seg] = cfg.segment_target_arpl(seg)
                elif metric_key == "ADS":
                    seg_targets[seg] = cfg.segment_target_ads(seg)
                elif metric_key == "LPD":
                    seg_targets[seg] = cfg.segment_target_lpd(seg)

            if metric_key == "ARPL":
                blended_target = cfg.blended_targets.get("arpl", 0)
            elif metric_key == "ADS":
                blended_target = cfg.blended_targets.get("ads", 0)
            elif metric_key == "LPD":
                blended_target = cfg.blended_targets.get("lpd", 0)

            for i, m in enumerate(MONTHS):
                ue = proc.monthly_unit_economics(m)
                row = current_row

                self._write_cell(ws, row, 1, MONTH_NAMES[i], bold=True)
                col = 2
                for seg in segs:
                    self._write_cell(ws, row, col, seg_targets[seg], fmt=fmt)
                    self._write_cell(ws, row, col + 1, ue[metric_key].get(seg, 0), fmt=fmt)
                    col += 2
                self._write_cell(ws, row, col, blended_target, fmt=fmt)
                self._write_cell(ws, row, col + 1, ue[metric_key].get("Blended", 0), fmt=fmt)

                current_row += 1

            # Blank row between sections
            current_row += 2

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws)
        freeze_panes(ws, row=3, col=2)
