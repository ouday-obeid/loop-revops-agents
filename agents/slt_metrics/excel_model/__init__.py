"""Excel workbook builder for the SLT revenue model.

Public entry point is `build(payload, output_path) -> Path`.

Internally, each sheet is a `BaseSheet` subclass living under
`excel_model/sheets/`. The builder orchestrates nine sheets in a fixed order
(Sheet 1 → 9) against a single `RevenueModelPayload`. Styles and chart helpers
are shared via `excel_model/styles.py` and `excel_model/helpers.py` so every
sheet reads from the same palette.

The slate-blue palette is a neutral default; swap to the Loop AI brand palette
in `styles.py` (one-file change) when the hex values land.
"""

from agents.slt_metrics.excel_model.builder import build

__all__ = ["build"]
