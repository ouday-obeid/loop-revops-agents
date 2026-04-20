"""Workbook assembly — single entry point `build(payload, output_path)`.

Loads every `BaseSheet` subclass in a fixed order, creates the openpyxl
`Workbook`, writes each sheet, and saves to disk. Callers are responsible
for passing a stable output path (the default convention is
``${REVOPS_REPO_ROOT}/var/reports/revenue_model/<YYYY-MM-DD>/Loop_Revenue_Model_<YYYY-MM-DD>.xlsx``).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from openpyxl import Workbook

from agents.slt_metrics.excel_model.sheets import BaseSheet
from agents.slt_metrics.excel_model.sheets.deal_details import DealDetailsSheet
from agents.slt_metrics.excel_model.sheets.ae_scorecard import AeScorecardSheet
from agents.slt_metrics.excel_model.sheets.sdr_scorecard import SdrScorecardSheet
from agents.slt_metrics.excel_model.sheets.unit_economics import UnitEconomicsSheet
from agents.slt_metrics.types import RevenueModelPayload

log = logging.getLogger(__name__)


def _default_sheet_order() -> list[BaseSheet]:
    """Sheets 1-4 ship in D12; sheets 5-9 are appended in D13 (see builder.py
    update). Keeping the registry as a function means late additions don't
    require import-order churn in other callers.
    """
    sheets: list[BaseSheet] = [
        DealDetailsSheet(),
        AeScorecardSheet(),
        SdrScorecardSheet(),
        UnitEconomicsSheet(),
    ]
    # Optional sheets — imported lazily so the builder still works if a
    # downstream sheet module isn't wired in yet.
    for factory_path in _LATE_SHEET_FACTORIES:
        try:
            module_path, class_name = factory_path.rsplit(".", 1)
            module = __import__(module_path, fromlist=[class_name])
            sheets.append(getattr(module, class_name)())
        except ImportError:
            log.debug("Skipping optional sheet %s — module not yet available", factory_path)
    return sheets


_LATE_SHEET_FACTORIES = (
    "agents.slt_metrics.excel_model.sheets.quota.QuotaSheet",
    "agents.slt_metrics.excel_model.sheets.pipeline_segment.PipelineSegmentSheet",
    "agents.slt_metrics.excel_model.sheets.deal_movers.DealMoversSheet",
    "agents.slt_metrics.excel_model.sheets.forecast_summary.ForecastSummarySheet",
    "agents.slt_metrics.excel_model.sheets.board_metrics.BoardMetricsSheet",
    "agents.slt_metrics.excel_model.sheets.expansion.ExpansionSheet",
    "agents.slt_metrics.excel_model.sheets.monthly_revenue.MonthlyRevenueSheet",
    "agents.slt_metrics.excel_model.sheets.funnel_metrics.FunnelMetricsSheet",
    "agents.slt_metrics.excel_model.sheets.rep_forecast.RepForecastSheet",
)


def build(
    payload: RevenueModelPayload,
    output_path: str | Path,
    *,
    sheets: Sequence[BaseSheet] | None = None,
) -> Path:
    """Render a `RevenueModelPayload` into an .xlsx file at `output_path`.

    Creates parent directories on demand. Returns the resolved output path.
    """
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    wb = Workbook()
    # Drop the default first sheet — every sheet below is explicit.
    default = wb.active
    wb.remove(default)

    registry = list(sheets) if sheets is not None else _default_sheet_order()
    if not registry:
        raise ValueError("Workbook must contain at least one sheet")

    for sheet in registry:
        ws = sheet.bind(wb)
        sheet.write(ws, payload)

    wb.save(path)
    log.info("Built revenue model workbook at %s (%d sheets)", path, len(registry))
    return path
