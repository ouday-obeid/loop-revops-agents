"""Raw Data sheet - processed SF data dump with auto-filter."""

from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from sheets.base import BaseSheet
from formatting.utils import auto_width, freeze_panes


# Columns to include and their display names + format
COLUMNS = [
    ("organization", "Organization", None),
    ("owner", "Opportunity Owner", None),
    ("opp_name", "Opportunity Name", None),
    ("brand", "Brand/Logo", None),
    ("record_type", "Record Type", None),
    ("stage", "Stage", None),
    ("stage_category", "Stage Category", None),
    ("stage_phase", "Pipeline Phase", None),
    ("acv", "Estimated ACV", "currency"),
    ("expansion_acv", "Expansion ACV", "currency"),
    ("weighted_acv", "Weighted ACV", "currency"),
    ("locations", "Locations", "number"),
    ("segment", "Segment", None),
    ("created_date", "Created Date", None),
    ("close_date", "Close Date", None),
    ("close_month", "Close Month", "number"),
    ("age", "Age (Days)", "number"),
    ("aging_bucket", "Aging Bucket", None),
    ("lead_source", "Lead Source", None),
    ("is_new_business", "New Business?", None),
    ("is_expansion", "Expansion?", None),
    ("is_closed_won", "Closed Won?", None),
    ("is_open", "Open Pipeline?", None),
]


class RawDataSheet(BaseSheet):
    sheet_name = "Raw Data"

    def _write(self, ws: Worksheet) -> None:
        df = self.proc.df

        # Header row
        headers = [c[1] for c in COLUMNS]
        self._write_headers(ws, 1, headers)

        # Data rows
        for r_idx, (_, row) in enumerate(df.iterrows(), start=2):
            for c_idx, (col_name, _, fmt) in enumerate(COLUMNS, start=1):
                val = row.get(col_name, "")
                # Convert pandas types to Python native
                if hasattr(val, "item"):
                    val = val.item()
                if str(val) == "nan" or str(val) == "NaT":
                    val = ""
                self._write_cell(ws, r_idx, c_idx, val, fmt=fmt)

        # Auto-filter
        ws.auto_filter.ref = f"A1:{chr(64 + len(COLUMNS))}{ws.max_row}"

    def _format(self, ws: Worksheet) -> None:
        auto_width(ws, min_width=10, max_width=40)
        freeze_panes(ws, row=2, col=1)
        self._alt_row_shading(ws, 2, ws.max_row)
