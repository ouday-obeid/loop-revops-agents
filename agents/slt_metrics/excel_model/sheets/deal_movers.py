"""Sheet 7 — Deal Movers.

Period from/to banner, then one row per mover ordered by |delta_acv|.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "Opp ID", "Opp Name", "Owner", "Kind",
    "Before", "After", "Δ ACV", "Δ Days",
)

_FORMATS = (
    None, None, None, None,
    None, None, S.FMT_MONEY, S.FMT_INT,
)


def _format_side(side: dict) -> str:
    """Compact `{k:v}` dict rendering for the Before/After columns."""
    if not side:
        return ""
    return "; ".join(f"{k}={v}" for k, v in side.items())


class DealMoversSheet(BaseSheet):
    sheet_name = "Deal Movers"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        ms = payload.movers
        H.write_title_banner(
            ws,
            f"Deal Movers · {ms.period_from.isoformat()} → {ms.period_to.isoformat()}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        # MoverSet.top() sorts by |delta_acv| descending already.
        movers = ms.top(n=max(100, len(ms.movers)))
        for i, mover in enumerate(movers, start=3):
            H.write_body_row(
                ws,
                row=i,
                values=(
                    mover.opp_id,
                    mover.opp_name,
                    mover.owner_name or "",
                    mover.kind,
                    _format_side(mover.before),
                    _format_side(mover.after),
                    mover.delta_acv if mover.delta_acv is not None else "—",
                    mover.delta_days if mover.delta_days is not None else "—",
                ),
                number_formats=list(_FORMATS),
            )

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
