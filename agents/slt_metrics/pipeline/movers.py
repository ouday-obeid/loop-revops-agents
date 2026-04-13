"""Diff two snapshots → MoverSet.

The detector operates on the dict rows returned by `snapshotter.read_snapshot`
so it's cheap to call from the briefing composer without rehydrating
ScoredDeal instances. Each Mover has a `kind` in one of:

    new          — appeared in `curr` but not `prev` (new pipeline)
    advanced     — stage rank strictly increased
    pushed       — close_date moved later by > threshold days
    slipped      — stage rank strictly decreased toward terminal
    lost         — stage moved into Closed Lost / Disqualified / No Show
    won          — stage moved into Closed Won
    amount_up    — ACV increased by > threshold $
    amount_down  — ACV decreased by > threshold $

The detector is deterministic: same (prev, curr) always yields the same
set. Callers can pass `thresholds` to tune sensitivity for different
briefing surfaces (daily vs weekly).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from agents.slt_metrics.pipeline.config import STAGE_RANK
from agents.slt_metrics.types import Mover, MoverSet

log = logging.getLogger(__name__)


# Default thresholds tuned to match scoping doc §Appendix C — "only flag
# movement worth reading about". AE scorecards can re-diff with smaller deltas.
DEFAULT_ACV_DELTA_THRESHOLD: float = 5_000.0
DEFAULT_CLOSE_DATE_PUSH_DAYS: int = 14

_TERMINAL_LOSS_STAGES: frozenset[str] = frozenset(
    {"Closed Lost", "Disqualified", "No Show"}
)
_TERMINAL_WIN_STAGES: frozenset[str] = frozenset({"Closed Won"})


@dataclass(frozen=True)
class MoverThresholds:
    acv_delta: float = DEFAULT_ACV_DELTA_THRESHOLD
    close_date_push_days: int = DEFAULT_CLOSE_DATE_PUSH_DAYS


def diff(
    prev: list[dict[str, Any]] | None,
    curr: list[dict[str, Any]],
    *,
    period_from: date,
    period_to: date,
    thresholds: MoverThresholds | None = None,
) -> MoverSet:
    """Compute the MoverSet between two snapshot row-lists.

    `prev=None` treats every row in `curr` as "new" — useful for the first
    snapshot of a quarter when there's no prior day to diff against.
    """
    t = thresholds or MoverThresholds()
    prev_map: Mapping[str, dict[str, Any]] = (
        {r["opp_id"]: r for r in prev} if prev else {}
    )
    curr_map: Mapping[str, dict[str, Any]] = {r["opp_id"]: r for r in curr}

    movers: list[Mover] = []
    for opp_id, curr_row in curr_map.items():
        prev_row = prev_map.get(opp_id)
        if prev_row is None:
            movers.append(_make_new(curr_row))
            continue
        movers.extend(_compare(prev_row, curr_row, t))

    # Opps present in prev but not curr: they slipped outside the fetch horizon
    # (closed, or close-date moved past NEXT_QUARTER). We emit a lost/won row
    # only if the last known stage was terminal; otherwise stay silent —
    # the morning fetcher's WHERE IsClosed=false naturally drops closed deals
    # so we don't want to double-flag.
    for opp_id, prev_row in prev_map.items():
        if opp_id in curr_map:
            continue
        mover = _dropped_from_fetch(prev_row)
        if mover is not None:
            movers.append(mover)

    return MoverSet(period_from=period_from, period_to=period_to, movers=movers)


# ------------------------------------------------------------------ helpers

def _make_new(curr: dict[str, Any]) -> Mover:
    return Mover(
        opp_id=curr["opp_id"],
        opp_name=_opp_name(curr),
        owner_name=curr.get("owner_name"),
        kind="new",
        before={},
        after=_summary(curr),
        delta_acv=_coerce_float(curr.get("acv")),
        delta_days=None,
    )


def _compare(prev: dict[str, Any], curr: dict[str, Any], t: MoverThresholds) -> list[Mover]:
    out: list[Mover] = []

    prev_stage = prev.get("stage") or ""
    curr_stage = curr.get("stage") or ""
    if prev_stage != curr_stage:
        out.append(_stage_mover(prev, curr, prev_stage, curr_stage))

    prev_acv = _coerce_float(prev.get("acv"))
    curr_acv = _coerce_float(curr.get("acv"))
    if prev_acv is not None and curr_acv is not None:
        delta = curr_acv - prev_acv
        if abs(delta) >= t.acv_delta:
            out.append(_amount_mover(prev, curr, delta))

    prev_close = _coerce_date(prev.get("close_date"))
    curr_close = _coerce_date(curr.get("close_date"))
    if prev_close is not None and curr_close is not None:
        delta_days = (curr_close - prev_close).days
        if delta_days >= t.close_date_push_days:
            out.append(
                Mover(
                    opp_id=curr["opp_id"],
                    opp_name=_opp_name(curr),
                    owner_name=curr.get("owner_name"),
                    kind="pushed",
                    before={"close_date": prev_close.isoformat()},
                    after={"close_date": curr_close.isoformat()},
                    delta_acv=curr_acv,
                    delta_days=delta_days,
                )
            )

    return out


def _stage_mover(
    prev: dict[str, Any], curr: dict[str, Any], prev_stage: str, curr_stage: str
) -> Mover:
    curr_rank = STAGE_RANK.get(curr_stage)
    prev_rank = STAGE_RANK.get(prev_stage)

    if curr_stage in _TERMINAL_WIN_STAGES:
        kind = "won"
    elif curr_stage in _TERMINAL_LOSS_STAGES:
        kind = "lost"
    elif prev_rank is not None and curr_rank is not None:
        kind = "advanced" if curr_rank > prev_rank else "slipped"
    else:
        kind = "advanced"  # unknown-stage fallback — treat forward motion

    return Mover(
        opp_id=curr["opp_id"],
        opp_name=_opp_name(curr),
        owner_name=curr.get("owner_name"),
        kind=kind,
        before={"stage": prev_stage},
        after={"stage": curr_stage},
        delta_acv=_coerce_float(curr.get("acv")),
        delta_days=None,
    )


def _amount_mover(prev: dict[str, Any], curr: dict[str, Any], delta: float) -> Mover:
    return Mover(
        opp_id=curr["opp_id"],
        opp_name=_opp_name(curr),
        owner_name=curr.get("owner_name"),
        kind="amount_up" if delta > 0 else "amount_down",
        before={"acv": prev.get("acv")},
        after={"acv": curr.get("acv")},
        delta_acv=delta,
        delta_days=None,
    )


def _dropped_from_fetch(prev: dict[str, Any]) -> Mover | None:
    stage = prev.get("stage") or ""
    if stage in _TERMINAL_WIN_STAGES:
        return Mover(
            opp_id=prev["opp_id"], opp_name=_opp_name(prev),
            owner_name=prev.get("owner_name"), kind="won",
            before={"stage": stage}, after={"stage": stage, "dropped": True},
            delta_acv=_coerce_float(prev.get("acv")),
        )
    if stage in _TERMINAL_LOSS_STAGES:
        return Mover(
            opp_id=prev["opp_id"], opp_name=_opp_name(prev),
            owner_name=prev.get("owner_name"), kind="lost",
            before={"stage": stage}, after={"stage": stage, "dropped": True},
            delta_acv=_coerce_float(prev.get("acv")),
        )
    return None


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": row.get("stage"),
        "acv": row.get("acv"),
        "close_date": row.get("close_date"),
        "segment": row.get("segment"),
    }


def _opp_name(row: dict[str, Any]) -> str:
    meta = row.get("metadata") or {}
    if isinstance(meta, dict):
        sf_raw = meta.get("sf_raw") or {}
        name = sf_raw.get("Name") if isinstance(sf_raw, dict) else None
        if name:
            return name
    return row.get("opp_id", "")


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
