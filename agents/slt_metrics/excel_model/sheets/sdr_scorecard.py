"""Sheet 3 — SDR Scorecard (new in Phase 6; not in Outbounder)."""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "SDR Email", "SDR Name",
    "Dials", "Connects", "Meetings Set", "Meetings Held",
    "Pipeline Sourced", "Pipeline Advanced", "Leaderboard Rank",
)

_FORMATS = (
    None, None,
    S.FMT_INT, S.FMT_INT, S.FMT_INT, S.FMT_INT,
    S.FMT_MONEY, S.FMT_MONEY, S.FMT_INT,
)


class SdrScorecardSheet(BaseSheet):
    sheet_name = "SDR Scorecard"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"SDR Scorecard · {payload.horizon_quarter} · {payload.run_date.isoformat()}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        # Leaderboard ordering first (1, 2, 3, …); None ranks sink to the bottom.
        cards = sorted(
            payload.sdr_cards,
            key=lambda c: (c.leaderboard_rank if c.leaderboard_rank is not None else 1_000_000),
        )
        for i, card in enumerate(cards, start=3):
            H.write_body_row(
                ws,
                row=i,
                values=(
                    card.sdr_email,
                    card.sdr_name or "",
                    card.dials if card.dials is not None else "—",
                    card.connects if card.connects is not None else "—",
                    card.meetings_set,
                    card.meetings_held,
                    card.pipeline_sourced,
                    card.pipeline_advanced,
                    card.leaderboard_rank if card.leaderboard_rank is not None else "—",
                ),
                number_formats=list(_FORMATS),
            )

        if cards:
            last_row = 2 + len(cards)
            H.conditional_color_scale(ws, f"G3:G{last_row}")

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
