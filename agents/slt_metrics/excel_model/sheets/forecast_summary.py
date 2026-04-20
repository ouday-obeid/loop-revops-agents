"""Sheet 8 — Forecast Summary.

Top block: quarter-level commit / best-case / weighted.
Middle block: by-segment rollup.
Bottom block: by-owner rollup (top 20 owners by commit).
"""
from __future__ import annotations

from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.excel_model import helpers as H, styles as S
from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.pipeline.config import (
    BEST_CASE_SCORE_THRESHOLD,
    COMMIT_SCORE_THRESHOLD,
)
from agents.slt_metrics.types import RevenueModelPayload


def _commit_of(vals: dict) -> float:
    """Accept either canonical keys (`commit_amount`) or shorthand (`commit`)."""
    return float(vals.get("commit_amount", vals.get("commit", 0.0)) or 0.0)


def _best_of(vals: dict) -> float:
    return float(vals.get("best_case_amount", vals.get("best_case", 0.0)) or 0.0)


def _weighted_of(vals: dict) -> float:
    return float(vals.get("weighted_amount", vals.get("weighted", 0.0)) or 0.0)


class ForecastSummarySheet(BaseSheet):
    sheet_name = "Forecast Summary"

    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        rollup = payload.forecast_rollup
        H.write_title_banner(
            ws,
            f"Forecast Summary · {rollup.horizon_quarter} · {payload.run_date.isoformat()}",
            cols=4,
        )

        # --- quarter-level ---
        H.write_header_row(ws, row=2, headers=["Metric", "Amount", "Weights", "Deal Count"])
        H.write_body_row(
            ws, row=3,
            values=(
                f"Commit (score ≥ {COMMIT_SCORE_THRESHOLD})",
                rollup.commit_amount, payload.weights.version, rollup.deal_count,
            ),
            number_formats=(None, S.FMT_MONEY, None, S.FMT_INT),
        )
        H.write_body_row(
            ws, row=4,
            values=(
                f"Best Case (score ≥ {BEST_CASE_SCORE_THRESHOLD})",
                rollup.best_case_amount, "", "",
            ),
            number_formats=(None, S.FMT_MONEY, None, None),
        )
        H.write_body_row(
            ws, row=5,
            values=("Weighted (Σ ACV × P)", rollup.weighted_amount, "", ""),
            number_formats=(None, S.FMT_MONEY, None, None),
        )

        # --- by segment ---
        H.write_header_row(ws, row=7, headers=["Segment", "Commit", "Best Case", "Weighted"])
        segments = sorted(rollup.by_segment.items(), key=lambda kv: -_commit_of(kv[1]))
        row = 8
        for seg, vals in segments:
            H.write_body_row(
                ws, row=row,
                values=(seg, _commit_of(vals), _best_of(vals), _weighted_of(vals)),
                number_formats=(None, S.FMT_MONEY, S.FMT_MONEY, S.FMT_MONEY),
            )
            row += 1

        # --- by owner (top 20) ---
        H.write_header_row(ws, row=row + 1, headers=["Owner", "Commit", "Best Case", "Weighted"])
        row += 2
        owners = sorted(rollup.by_owner.items(), key=lambda kv: -_commit_of(kv[1]))[:20]
        for owner, vals in owners:
            H.write_body_row(
                ws, row=row,
                values=(owner, _commit_of(vals), _best_of(vals), _weighted_of(vals)),
                number_formats=(None, S.FMT_MONEY, S.FMT_MONEY, S.FMT_MONEY),
            )
            row += 1

        H.freeze_header(ws, rows=2)
        H.auto_width(ws)
