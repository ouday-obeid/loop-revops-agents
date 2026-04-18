"""Sheet — Rep Forecast.

Per-AE roster grouped by manager. For each rep: annual quota, QTD closed-won
(from `closed_opps_quarter`), open pipeline (from `scored_deals`), and a
blank `Rep Submitted Forecast` column — populated in step 3 when the
`@oo slt ingest-rep-forecast` command lands.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.pipeline.planning import (
    AE_ROSTER,
    MANAGER_GROUPS,
    RosterEntry,
    manager_for_ae,
)
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "Rep",
    "Segment", "Status",
    "Annual Quota",
    "QTD Won ACV", "QTD Attainment (of Q)",
    "Open Pipeline",
    "Rep Submitted Forecast",
)
_FORMATS = (
    None,
    None, None,
    S.FMT_MONEY,
    S.FMT_MONEY, S.FMT_PCT,
    S.FMT_MONEY,
    S.FMT_MONEY,
)


def _quarterly_quota(annual: float) -> float:
    return annual / 4.0 if annual else 0.0


def _won_acv_by_owner(payload: RevenueModelPayload) -> dict[str, float]:
    out: dict[str, float] = {}
    for opp in payload.closed_opps_quarter:
        if not opp.is_won or not opp.owner_name:
            continue
        out[opp.owner_name] = out.get(opp.owner_name, 0.0) + (opp.acv or 0.0)
    return out


def _open_pipeline_by_owner(payload: RevenueModelPayload) -> dict[str, float]:
    out: dict[str, float] = {}
    for deal in payload.scored_deals:
        if not deal.owner_name:
            continue
        out[deal.owner_name] = out.get(deal.owner_name, 0.0) + (deal.acv or 0.0)
    return out


def _write_manager_header(ws: Worksheet, row: int, manager: str) -> None:
    cell = ws.cell(row=row, column=1, value=f"Manager · {manager}")
    cell.font = S.FONT_BODY_BOLD
    cell.fill = S.FILL_ALT_ROW


def _group_roster() -> dict[str, list[RosterEntry]]:
    groups: dict[str, list[RosterEntry]] = {mgr: [] for mgr in MANAGER_GROUPS}
    groups["Unassigned"] = []
    for entry in AE_ROSTER:
        mgr = manager_for_ae(entry.name)
        groups.setdefault(mgr, []).append(entry)
    return groups


class RepForecastSheet(BaseSheet):
    sheet_name = "Rep Forecast"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Rep Forecast · {payload.horizon_quarter}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        won_by_owner = _won_acv_by_owner(payload)
        open_by_owner = _open_pipeline_by_owner(payload)
        grouped = _group_roster()

        row = 3
        for manager, members in grouped.items():
            if not members:
                continue
            _write_manager_header(ws, row, manager)
            row += 1
            for entry in members:
                q_quota = _quarterly_quota(entry.annual_quota)
                won = won_by_owner.get(entry.name, 0.0)
                attainment = (won / q_quota) if q_quota else 0.0
                open_pipe = open_by_owner.get(entry.name, 0.0)
                H.write_body_row(
                    ws, row=row,
                    values=(
                        entry.name,
                        entry.segment, entry.status,
                        entry.annual_quota,
                        won, attainment,
                        open_pipe,
                        None,
                    ),
                    number_formats=list(_FORMATS),
                )
                row += 1

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
