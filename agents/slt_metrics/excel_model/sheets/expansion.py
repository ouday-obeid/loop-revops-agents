"""Sheet — Expansion (monthly target vs actual).

Reads `aggregates.monthly_closed_won_by_kind` for actuals and
`planning.MONTHLY_TARGETS` for targets. One row per month, trailing Total.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import aggregates, helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.pipeline.planning import MONTHLY_TARGETS
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = ("Month", "Target Expansion", "Actual Expansion", "Δ", "Attainment")
_FORMATS = (None, S.FMT_MONEY, S.FMT_MONEY, S.FMT_MONEY, S.FMT_PCT)

_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


class ExpansionSheet(BaseSheet):
    sheet_name = "Expansion"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Expansion · {payload.run_date.year} · target vs actual",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        actuals = aggregates.monthly_closed_won_by_kind(payload.closed_opps_quarter)

        total_target = 0.0
        total_actual = 0.0
        for i, month in enumerate(range(1, 13), start=3):
            target = MONTHLY_TARGETS[month].expansion
            actual = actuals.get(month, {}).get("expansion", 0.0)
            delta = actual - target
            attainment = (actual / target) if target else 0.0
            H.write_body_row(
                ws,
                row=i,
                values=(_MONTH_NAMES[month - 1], target, actual, delta, attainment),
                number_formats=list(_FORMATS),
            )
            total_target += target
            total_actual += actual

        total_delta = total_actual - total_target
        total_attainment = (total_actual / total_target) if total_target else 0.0
        H.write_body_row(
            ws,
            row=15,
            values=("Total", total_target, total_actual, total_delta, total_attainment),
            number_formats=list(_FORMATS),
        )

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
