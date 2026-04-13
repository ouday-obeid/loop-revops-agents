"""BaseSheet ABC. Each concrete sheet writes itself into an openpyxl workbook
from a single `RevenueModelPayload`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet

from agents.slt_metrics.types import RevenueModelPayload


class BaseSheet(ABC):
    """Sheet subclasses implement `sheet_name` and `write(ws, payload)`.

    The builder creates the worksheet (via `wb.create_sheet(sheet_name)`),
    then hands it to `write()`. Subclasses are responsible for row layout,
    headers, styling (via helpers), and charts.
    """

    sheet_name: str = ""

    def bind(self, wb: Workbook) -> Worksheet:
        if not self.sheet_name:
            raise ValueError(f"{type(self).__name__}.sheet_name is empty")
        return wb.create_sheet(self.sheet_name)

    @abstractmethod
    def write(self, ws: Worksheet, payload: RevenueModelPayload) -> None:
        ...
