"""Sheet — Monthly Revenue.

Full monthly revenue view: target vs actual for new-biz, expansion, and
total, with attainment columns. One row per month, trailing Total.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import aggregates, helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.pipeline.planning import MONTHLY_TARGETS
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "Month",
    "Target New Biz", "Actual New Biz", "New Biz Attainment",
    "Target Expansion", "Actual Expansion", "Expansion Attainment",
    "Target Total", "Actual Total", "Total Attainment",
)
_FORMATS = (
    None,
    S.FMT_MONEY, S.FMT_MONEY, S.FMT_PCT,
    S.FMT_MONEY, S.FMT_MONEY, S.FMT_PCT,
    S.FMT_MONEY, S.FMT_MONEY, S.FMT_PCT,
)

_MONTH_NAMES = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _attainment(actual: float, target: float) -> float:
    return (actual / target) if target else 0.0


class MonthlyRevenueSheet(BaseSheet):
    sheet_name = "Monthly Revenue"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Monthly Revenue · {payload.run_date.year} · target vs actual",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        actuals = aggregates.monthly_closed_won_by_kind(payload.closed_opps_quarter)

        totals = {
            "target_nb": 0.0, "actual_nb": 0.0,
            "target_ex": 0.0, "actual_ex": 0.0,
        }
        for i, month in enumerate(range(1, 13), start=3):
            target_nb = MONTHLY_TARGETS[month].new_biz
            target_ex = MONTHLY_TARGETS[month].expansion
            bucket = actuals.get(month, {})
            actual_nb = bucket.get("new_biz", 0.0)
            actual_ex = bucket.get("expansion", 0.0)
            H.write_body_row(
                ws,
                row=i,
                values=(
                    _MONTH_NAMES[month - 1],
                    target_nb, actual_nb, _attainment(actual_nb, target_nb),
                    target_ex, actual_ex, _attainment(actual_ex, target_ex),
                    target_nb + target_ex, actual_nb + actual_ex,
                    _attainment(actual_nb + actual_ex, target_nb + target_ex),
                ),
                number_formats=list(_FORMATS),
            )
            totals["target_nb"] += target_nb
            totals["actual_nb"] += actual_nb
            totals["target_ex"] += target_ex
            totals["actual_ex"] += actual_ex

        target_total = totals["target_nb"] + totals["target_ex"]
        actual_total = totals["actual_nb"] + totals["actual_ex"]
        H.write_body_row(
            ws,
            row=15,
            values=(
                "Total",
                totals["target_nb"], totals["actual_nb"],
                _attainment(totals["actual_nb"], totals["target_nb"]),
                totals["target_ex"], totals["actual_ex"],
                _attainment(totals["actual_ex"], totals["target_ex"]),
                target_total, actual_total,
                _attainment(actual_total, target_total),
            ),
            number_formats=list(_FORMATS),
        )

        H.add_bar_chart(
            ws,
            title="Monthly Target vs Actual (Total)",
            data_ref=f"H2:I14",
            categories_ref=f"A3:A14",
            anchor="L3",
        )

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
