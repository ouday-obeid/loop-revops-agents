"""ARR, NRR, logo retention, expansion — SF-driven with BQ polish.

SF gives us the `fixed_arr` field on closed-won opps (our canonical ARR
number) plus churn / downgrade events. When a Loop Pulse NRR/GRR/logo row is
available, those values override the SF rollup — BQ has richer cohort-level
visibility than the SF rollup can express.

All functions are pure; the caller is responsible for pulling SF + BQ and
handing over already-loaded records.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from agents.slt_metrics.types import OppRecord, UnitEconomics


@dataclass(frozen=True)
class ArrNrrSnapshot:
    """Inputs used by board_summary. Everything is optional: NRR/logo/
    expansion can all be None if BQ is down AND the SF fallback can't
    confidently compute them."""
    as_of: date
    arr: float | None
    nrr: float | None
    logo_retention: float | None
    expansion_rate: float | None


def build_arr_nrr(
    *,
    as_of: date,
    closed_opps: Iterable[OppRecord],
    unit_economics: UnitEconomics,
) -> ArrNrrSnapshot:
    """Compose the ARR/NRR snapshot.

    - `arr` always comes from the SF closed-won rollup (fixed_arr when present,
      acv otherwise). The SF number is authoritative because Loop Pulse's
      `arr_current` lags the revenue sync by up to 24h.
    - NRR / GRR / logo / expansion prefer Loop Pulse when healthy, fall back
      to None when the unit-econ card is gap-flagged.
    """
    arr = _sum_won_arr(closed_opps)
    nrr = unit_economics.net_revenue_retention if not unit_economics.gap_flag else None
    logo = unit_economics.logo_retention if not unit_economics.gap_flag else None
    expansion = unit_economics.expansion_rate if not unit_economics.gap_flag else None

    return ArrNrrSnapshot(
        as_of=as_of,
        arr=arr,
        nrr=nrr,
        logo_retention=logo,
        expansion_rate=expansion,
    )


def _sum_won_arr(opps: Iterable[OppRecord]) -> float | None:
    total = 0.0
    counted = 0
    for o in opps:
        if not o.is_won:
            continue
        value = o.fixed_arr if o.fixed_arr is not None else o.acv
        if value is None:
            continue
        total += float(value)
        counted += 1
    return total if counted else None
