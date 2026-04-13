"""Sheet 2 — AE Scorecard.

One row per AE. Attainment, close rate, cycle, avg ACV, pipeline created /
advanced, call grade average, rep-perf composite, open and commit counts.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "Rep Email", "Rep Name",
    "Attainment %", "Close Rate %", "Avg Cycle (days)", "Avg ACV",
    "Pipeline Created", "Pipeline Advanced",
    "Call Grade Avg", "Rep Perf Score", "Deals Open", "Deals Commit",
)

_FORMATS = (
    None, None,
    S.FMT_PCT, S.FMT_PCT, S.FMT_RATIO, S.FMT_MONEY,
    S.FMT_MONEY, S.FMT_MONEY,
    S.FMT_RATIO, S.FMT_INT, S.FMT_INT, S.FMT_INT,
)


class AeScorecardSheet(BaseSheet):
    sheet_name = "AE Scorecard"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"AE Scorecard · {payload.horizon_quarter} · {payload.run_date.isoformat()}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        cards = sorted(
            payload.ae_cards,
            key=lambda c: (c.attainment_pct or 0),
            reverse=True,
        )
        for i, card in enumerate(cards, start=3):
            H.write_body_row(
                ws,
                row=i,
                values=(
                    card.rep_email,
                    card.rep_name or "",
                    H.pct(card.attainment_pct),
                    H.pct(card.close_rate_pct),
                    H.ratio(card.avg_cycle_days),
                    H.money(card.avg_acv),
                    card.pipeline_created,
                    card.pipeline_advanced,
                    H.ratio(card.call_grade_avg),
                    card.rep_perf_score if card.rep_perf_score is not None else "—",
                    card.deals_open,
                    card.deals_commit,
                ),
                number_formats=list(_FORMATS),
            )

        if cards:
            last_row = 2 + len(cards)
            # Color the Attainment column so outliers pop.
            H.conditional_color_scale(ws, f"C3:C{last_row}")

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
