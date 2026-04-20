"""Sheet 5 — Quota attainment and pacing.

Per-AE quota + attained + gap to straight-line pacing. Straight-line pacing
is inferred from `forecast_rollup.commit_amount` and `ae_card.attainment_pct`
— we don't attempt to model quarter-day progress here; a future pass can
plug in `quota.pacing_vs_straight_line()` once `run_date` is confirmed to
fall inside a single quarter window.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.pipeline.planning import AE_ROSTER
from agents.slt_metrics.types import RevenueModelPayload


_HEADERS = (
    "Rep Email", "Rep Name", "Annual Quota",
    "Attainment %", "Commit Pipeline", "Pipeline Created", "Pipeline Advanced",
    "Deals Open", "Deals Commit",
)

_FORMATS = (
    None, None, S.FMT_MONEY,
    S.FMT_PCT, S.FMT_MONEY, S.FMT_MONEY, S.FMT_MONEY,
    S.FMT_INT, S.FMT_INT,
)


_ANNUAL_QUOTA_BY_NAME = {entry.name: entry.annual_quota for entry in AE_ROSTER}


class QuotaSheet(BaseSheet):
    sheet_name = "Quota"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Quota & Pacing · {payload.horizon_quarter}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        commit_by_owner = {
            owner: float(vals.get("commit_amount", vals.get("commit", 0.0)) or 0.0)
            for owner, vals in payload.forecast_rollup.by_owner.items()
        }
        cards = sorted(payload.ae_cards, key=lambda c: (c.attainment_pct or 0), reverse=True)

        for i, card in enumerate(cards, start=3):
            commit = commit_by_owner.get(card.rep_email, commit_by_owner.get(card.rep_name or "", 0.0))
            annual_quota = _ANNUAL_QUOTA_BY_NAME.get(card.rep_name or "", None)
            H.write_body_row(
                ws,
                row=i,
                values=(
                    card.rep_email,
                    card.rep_name or "",
                    H.money(annual_quota),
                    H.pct(card.attainment_pct),
                    commit,
                    card.pipeline_created,
                    card.pipeline_advanced,
                    card.deals_open,
                    card.deals_commit,
                ),
                number_formats=list(_FORMATS),
            )

        if cards:
            last_row = 2 + len(cards)
            # Attainment column shifted from C to D after inserting Annual Quota.
            H.conditional_color_scale(ws, f"D3:D{last_row}")

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
