"""Sheet 4 — Unit Economics.

Populated from Loop Pulse (BigQuery) when available; emits a fully-formed
gap-flag sheet when BQ is disconnected so the reader always sees the cause.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.types import RevenueModelPayload, UnitEconomics


_METRIC_ROWS = (
    ("Gross Revenue Retention",  "gross_revenue_retention", S.FMT_PCT),
    ("Net Revenue Retention",    "net_revenue_retention",   S.FMT_PCT),
    ("Logo Retention",           "logo_retention",          S.FMT_PCT),
    ("Expansion Rate",           "expansion_rate",          S.FMT_PCT),
    ("CAC Payback (months)",     "cac_payback_months",      S.FMT_RATIO),
    ("LTV : CAC Ratio",          "ltv_cac_ratio",           S.FMT_RATIO),
)


def _value(ue: UnitEconomics, attr: str) -> float | str:
    v = getattr(ue, attr, None)
    return v if v is not None else "—"


class UnitEconomicsSheet(BaseSheet):
    sheet_name = "Unit Economics"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        ue = payload.board_metrics.unit_economics
        H.write_title_banner(
            ws,
            f"Unit Economics · {payload.run_date.isoformat()}",
            cols=3,
        )
        H.write_header_row(ws, row=2, headers=["Metric", "Value", "Gap Flag"])

        if ue.gap_flag:
            # Full gap-flag treatment — every value cell reads the same message.
            for i, (label, _attr, _fmt) in enumerate(_METRIC_ROWS, start=3):
                H.write_body_row(
                    ws,
                    row=i,
                    values=(label, "", "TRUE"),
                    number_formats=(None, None, None),
                )
                # Overwrite the value cell with the gap-flag banner.
                cell = ws.cell(row=i, column=2, value=S.GAP_TEXT)
                cell.font = S.FONT_GAP
                cell.fill = S.FILL_GAP
                cell.border = S.BORDER_CELL
            return

        for i, (label, attr, fmt) in enumerate(_METRIC_ROWS, start=3):
            H.write_body_row(
                ws,
                row=i,
                values=(label, _value(ue, attr), "FALSE"),
                number_formats=(None, fmt, None),
            )

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
