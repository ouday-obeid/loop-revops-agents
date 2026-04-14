"""Unit economics — GRR, NRR, logo retention, expansion, CAC payback, LTV:CAC.

Canonical source is Loop Pulse (BigQuery). Until credentials are wired
(`BQ_CREDENTIALS_JSON`), every cell returns None and `gap_flag=True` — the
Excel renderer pipes this through to `-- (Loop Pulse unavailable)` cells so
readers always see the cause rather than an empty sheet.

When a real BQ row is passed in we coerce the known field names from the
UNIT_ECONOMICS SQL constant — anything missing becomes None but does not
poison the rest of the card.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from agents.slt_metrics.types import UnitEconomics

log = logging.getLogger(__name__)


def build_unit_economics(bq_row: Mapping[str, Any] | None) -> UnitEconomics:
    """Convert a Loop Pulse unit-economics row into the `UnitEconomics` shape.

    Passing None (or an empty mapping) produces the gap-flagged version.
    """
    if not bq_row:
        return _gap_flagged()

    try:
        return UnitEconomics(
            gross_revenue_retention=_coerce_pct(bq_row.get("gross_revenue_retention")),
            net_revenue_retention=_coerce_pct(bq_row.get("net_revenue_retention")),
            logo_retention=_coerce_pct(bq_row.get("logo_retention")),
            expansion_rate=_coerce_pct(bq_row.get("expansion_rate")),
            cac_payback_months=_coerce_float(bq_row.get("cac_payback_months")),
            ltv_cac_ratio=_coerce_float(bq_row.get("ltv_cac_ratio")),
            gap_flag=False,
        )
    except Exception:
        log.exception("build_unit_economics: malformed BQ row — falling back to gap-flag")
        return _gap_flagged()


def _gap_flagged() -> UnitEconomics:
    return UnitEconomics(
        gross_revenue_retention=None,
        net_revenue_retention=None,
        logo_retention=None,
        expansion_rate=None,
        cac_payback_months=None,
        ltv_cac_ratio=None,
        gap_flag=True,
    )


def _coerce_pct(raw: Any) -> float | None:
    """Loop Pulse returns 0.0–1.0 pct directly — None-safe."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _coerce_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None
