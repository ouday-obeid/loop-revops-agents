"""Mover detector — stage transitions, ACV deltas, close-date pushes,
drop-from-fetch edge cases, threshold tuning, top(n) ordering.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from agents.slt_metrics.pipeline import movers
from agents.slt_metrics.pipeline.movers import MoverThresholds


def _row(
    opp_id: str = "0061x100",
    stage: str = "Proposal",
    acv: float | None = 90000.0,
    close_date: str | date | None = "2026-06-30",
    owner_name: str = "Nate",
    name: str | None = "Test Opp",
) -> dict[str, Any]:
    return {
        "opp_id": opp_id,
        "stage": stage,
        "acv": acv,
        "close_date": close_date,
        "owner_name": owner_name,
        "segment": "MM",
        "metadata": {"sf_raw": {"Name": name}} if name else {},
    }


_PERIOD_FROM = date(2026, 4, 12)
_PERIOD_TO = date(2026, 4, 13)


# ------------------------------------------------------------------ new / unchanged

def test_new_opp_when_absent_from_prev():
    curr = [_row("A"), _row("B")]
    prev: list[dict[str, Any]] = []
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert {m.opp_id: m.kind for m in ms.movers} == {"A": "new", "B": "new"}


def test_first_snapshot_with_prev_none_treats_all_as_new():
    curr = [_row("A")]
    ms = movers.diff(None, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["new"]


def test_identical_snapshots_produce_no_movers():
    prev = [_row("A"), _row("B")]
    curr = [_row("A"), _row("B")]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert ms.movers == []


# ------------------------------------------------------------------ stage changes

def test_advanced_stage_detected():
    prev = [_row("A", stage="Demo")]
    curr = [_row("A", stage="Business Case")]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["advanced"]


def test_slipped_stage_detected():
    prev = [_row("A", stage="Proposal")]
    curr = [_row("A", stage="Business Case")]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["slipped"]


def test_won_stage_detected():
    prev = [_row("A", stage="Proposal")]
    curr = [_row("A", stage="Closed Won")]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["won"]


@pytest.mark.parametrize("terminal", ["Closed Lost", "Disqualified", "No Show"])
def test_lost_terminals_detected(terminal):
    prev = [_row("A", stage="Demo")]
    curr = [_row("A", stage=terminal)]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["lost"]


# ------------------------------------------------------------------ amount deltas

def test_amount_up_above_threshold():
    prev = [_row("A", acv=90_000.0)]
    curr = [_row("A", acv=105_000.0)]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert len(ms.movers) == 1
    m = ms.movers[0]
    assert m.kind == "amount_up"
    assert m.delta_acv == 15_000.0


def test_amount_down_above_threshold():
    prev = [_row("A", acv=90_000.0)]
    curr = [_row("A", acv=70_000.0)]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["amount_down"]
    assert ms.movers[0].delta_acv == -20_000.0


def test_amount_delta_below_threshold_ignored():
    prev = [_row("A", acv=90_000.0)]
    curr = [_row("A", acv=91_000.0)]   # under $5k
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert ms.movers == []


def test_threshold_can_be_tuned():
    prev = [_row("A", acv=90_000.0)]
    curr = [_row("A", acv=91_000.0)]
    ms = movers.diff(
        prev, curr,
        period_from=_PERIOD_FROM, period_to=_PERIOD_TO,
        thresholds=MoverThresholds(acv_delta=500.0),
    )
    assert [m.kind for m in ms.movers] == ["amount_up"]


# ------------------------------------------------------------------ close-date pushes

def test_close_date_push_beyond_threshold():
    prev = [_row("A", close_date="2026-05-15")]
    curr = [_row("A", close_date="2026-06-05")]   # +21 days, > 14
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    kinds = [m.kind for m in ms.movers]
    assert "pushed" in kinds


def test_close_date_pulled_in_not_a_mover():
    prev = [_row("A", close_date="2026-06-30")]
    curr = [_row("A", close_date="2026-06-10")]   # pulled in
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    # No "pulled" kind by design — pulled-in deals aren't a risk signal.
    kinds = [m.kind for m in ms.movers]
    assert "pushed" not in kinds


def test_combined_stage_and_amount_moves_yield_two_movers():
    prev = [_row("A", stage="Demo", acv=90_000.0)]
    curr = [_row("A", stage="Proposal", acv=150_000.0)]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    kinds = [m.kind for m in ms.movers]
    assert set(kinds) == {"advanced", "amount_up"}


# ------------------------------------------------------------------ dropped from fetch

def test_dropped_closed_won_emits_won_mover():
    prev = [_row("A", stage="Closed Won")]
    curr: list[dict[str, Any]] = []
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert [m.kind for m in ms.movers] == ["won"]


def test_dropped_open_opp_silent():
    # An open opp falling outside the fetch horizon (close date beyond NEXT_QUARTER)
    # should NOT be flagged as a mover — the fetcher dropped it, not the rep.
    prev = [_row("A", stage="Proposal")]
    curr: list[dict[str, Any]] = []
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert ms.movers == []


# ------------------------------------------------------------------ MoverSet.top

def test_top_orders_by_absolute_delta_acv():
    prev = [
        _row("SMALL", acv=90_000.0),
        _row("LARGE", acv=200_000.0),
        _row("MID",   acv=100_000.0),
    ]
    curr = [
        _row("SMALL", acv=100_000.0),  # +10k
        _row("LARGE", acv=50_000.0),   # -150k
        _row("MID",   acv=130_000.0),  # +30k
    ]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    top = ms.top(n=2)
    # LARGE has biggest absolute delta, then MID.
    assert [m.opp_id for m in top] == ["LARGE", "MID"]


def test_opp_name_pulled_from_metadata():
    prev: list[dict[str, Any]] = []
    curr = [_row("A", name="CFA Proposal")]
    ms = movers.diff(prev, curr, period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert ms.movers[0].opp_name == "CFA Proposal"


def test_opp_name_falls_back_to_id_when_metadata_missing():
    row = _row("A", name=None)
    row["metadata"] = {}
    prev: list[dict[str, Any]] = []
    ms = movers.diff(prev, [row], period_from=_PERIOD_FROM, period_to=_PERIOD_TO)
    assert ms.movers[0].opp_name == "A"
