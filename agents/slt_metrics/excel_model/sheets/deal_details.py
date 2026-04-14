"""Sheet 1 — Deal Details.

One row per scored deal with full pillar breakdown, category, risk flags,
and weighted ACV. Sorted by weighted ACV descending so the biggest-impact
deals sit at the top.
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.types import RevenueModelPayload, ScoredDeal


_HEADERS = (
    "Opp ID", "Name", "Owner", "Account", "Segment", "Stage",
    "Amount", "ACV", "Close Date",
    "Score", "Category", "Probability", "Weighted ACV",
    "ICP", "Stage P", "Activity P", "Timeline P", "Call P",
    "Risk Flags", "Weights Version",
)

_FORMATS = (
    None, None, None, None, None, None,
    S.FMT_MONEY, S.FMT_MONEY, S.FMT_DATE,
    S.FMT_INT, None, S.FMT_PCT, S.FMT_MONEY,
    S.FMT_RATIO, S.FMT_RATIO, S.FMT_RATIO, S.FMT_RATIO, S.FMT_RATIO,
    None, None,
)


def _pillar(deal: ScoredDeal, key: str) -> float | None:
    p = deal.pillars.get(key)
    return p.value if p is not None else None


class DealDetailsSheet(BaseSheet):
    sheet_name = "Deal Details"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        H.write_title_banner(
            ws,
            f"Deal Details · {payload.horizon_quarter} · {payload.run_date.isoformat()}",
            cols=len(_HEADERS),
        )
        H.write_header_row(ws, row=2, headers=list(_HEADERS))

        deals = sorted(payload.scored_deals, key=lambda d: d.weighted_acv, reverse=True)
        for i, deal in enumerate(deals, start=3):
            H.write_body_row(
                ws,
                row=i,
                values=(
                    deal.opp_id,
                    deal.opp_name,
                    deal.owner_name or "",
                    deal.account_name or "",
                    deal.segment or "",
                    deal.stage,
                    H.money(deal.amount),
                    H.money(deal.acv),
                    deal.close_date,
                    deal.score,
                    deal.category,
                    deal.probability,
                    deal.weighted_acv,
                    H.ratio(_pillar(deal, "icp")),
                    H.ratio(_pillar(deal, "stage")),
                    H.ratio(_pillar(deal, "activity")),
                    H.ratio(_pillar(deal, "timeline")),
                    H.ratio(_pillar(deal, "call")),
                    ", ".join(deal.risk_flags),
                    deal.weights_version,
                ),
                number_formats=list(_FORMATS),
            )

        # Color-scale the Score column so outliers are instantly visible.
        if deals:
            last_row = 2 + len(deals)
            H.conditional_color_scale(ws, f"J3:J{last_row}")

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
