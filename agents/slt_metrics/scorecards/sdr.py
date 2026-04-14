"""SDR (Sales Development) scorecard builder.

Mirrors `scorecards/ae.py` but for the top-of-funnel roles:
  - dials / connects   — from Nooks (placeholder until wired)
  - meetings_set       — count of meetings booked in lookback
  - meetings_held      — count with Held status
  - pipeline_sourced   — Σ ACV of opps the SDR sourced inside the window
  - pipeline_advanced  — Σ ACV of sourced opps that progressed
  - leaderboard_rank   — 1..N by pipeline_sourced, ties broken by meetings_held

Inputs are deliberately dumb — the caller pre-groups the meetings + sourced
opps so this module stays free of SOQL / Nooks imports.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from agents.slt_metrics.scorecards.quota import RepConfig
from agents.slt_metrics.types import OppRecord, SdrCard


@dataclass
class NooksActivity:
    sdr_name: str
    dials: int | None
    connects: int | None


@dataclass
class SdrMeeting:
    sdr_name: str
    held: bool


def build_sdr_cards(
    *,
    meetings: Iterable[SdrMeeting],
    sourced_opps: Iterable[OppRecord],
    advanced_opp_ids: Iterable[str],
    nooks: Iterable[NooksActivity] | None = None,
    rep_configs: Iterable[RepConfig],
) -> list[SdrCard]:
    """Build one SdrCard per SDR. Ordering: descending by pipeline_sourced."""
    sdr_names = {r.owner_name for r in rep_configs if (r.role or "").upper() == "SDR"}

    meetings_list = list(meetings)
    sourced_list = list(sourced_opps)
    advanced_set = set(advanced_opp_ids)

    # Pull in any SDR who shows up in activity but isn't in rep_configs — the
    # roster is the source of truth, but we still want to surface unknowns.
    for m in meetings_list:
        sdr_names.add(m.sdr_name)
    for o in sourced_list:
        if o.owner_name:
            sdr_names.add(o.owner_name)

    nooks_by_sdr: dict[str, NooksActivity] = {
        n.sdr_name: n for n in (nooks or [])
    }

    by_sdr_meetings: dict[str, list[SdrMeeting]] = {}
    for m in meetings_list:
        by_sdr_meetings.setdefault(m.sdr_name, []).append(m)

    by_sdr_sourced: dict[str, list[OppRecord]] = {}
    for o in sourced_list:
        if o.owner_name:
            by_sdr_sourced.setdefault(o.owner_name, []).append(o)

    raw: list[SdrCard] = []
    for name in sorted(sdr_names):
        ms = by_sdr_meetings.get(name, [])
        sourced = by_sdr_sourced.get(name, [])
        meetings_set = len(ms)
        meetings_held = sum(1 for m in ms if m.held)
        pipeline_sourced = sum((o.acv or 0.0) for o in sourced)
        pipeline_advanced = sum(
            (o.acv or 0.0)
            for o in sourced
            if o.id in advanced_set
        )
        nook = nooks_by_sdr.get(name)
        raw.append(
            SdrCard(
                sdr_email=_sdr_email_placeholder(name),
                sdr_name=name,
                dials=(nook.dials if nook else None),
                connects=(nook.connects if nook else None),
                meetings_set=meetings_set,
                meetings_held=meetings_held,
                pipeline_sourced=pipeline_sourced,
                pipeline_advanced=pipeline_advanced,
                leaderboard_rank=None,  # set below
            )
        )

    ranked = sorted(
        raw,
        key=lambda c: (-c.pipeline_sourced, -c.meetings_held, c.sdr_name or ""),
    )
    for rank, card in enumerate(ranked, start=1):
        card.leaderboard_rank = rank
    return ranked


def _sdr_email_placeholder(sdr_name: str) -> str:
    slug = sdr_name.lower().replace(" ", ".").replace("'", "")
    return f"{slug}@tryloop.ai"
