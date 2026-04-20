"""Pure-data aggregators over `RevenueModelPayload.closed_opps_quarter` and
`RevenueModelPayload.all_opps_snapshot`.

Every function here is stateless: it takes a list of `OppRecord` and returns
a dict/list of primitives. Sheets consume the output; briefings may too once
they want monthly/segment tables. No I/O, no DB, no SF calls.

Keying conventions:
  * `closed_opps_quarter` is already scoped to a single quarter by the
    fetcher, so month keys (1..12) are unambiguous.
  * `all_opps_snapshot` spans the trailing 12 months + open pipeline — when
    a function groups by time, it keys by `(year, month)` tuples to avoid
    collapsing 2025 and 2026 data together.
"""
from __future__ import annotations

from typing import Any

from agents.slt_metrics.pipeline.config import segment_for_acv
from agents.slt_metrics.types import OppRecord


# ---------------------------------------------------------------- classification

_EXPANSION_MARKERS = ("expan", "upsell", "upgrade", "renewal")
_NEW_BIZ_MARKERS = ("new",)


def classify_opp_kind(type_: str | None) -> str:
    """Map SF `Opportunity.Type` to one of `new_biz` | `expansion` | `other`.

    Case-insensitive substring match — Loop's historical picklist mixes
    "New Business" with "Existing Customer - Upgrade" and similar.
    """
    if not type_:
        return "other"
    t = type_.lower()
    if any(m in t for m in _EXPANSION_MARKERS):
        return "expansion"
    if any(m in t for m in _NEW_BIZ_MARKERS):
        return "new_biz"
    return "other"


# ---------------------------------------------------------------- monthly closed-won

def monthly_closed_won_by_kind(
    closed_opps: list[OppRecord],
) -> dict[int, dict[str, float]]:
    """Closed-won ACV per month, split by kind (new_biz / expansion / other).

    Input is expected to be a single-quarter slice (payload.closed_opps_quarter).
    Months are keyed 1..12. Only `is_won=True` rows contribute; lost opps
    have their own counter in `quarterly_closed_by_segment`.
    """
    out: dict[int, dict[str, float]] = {}
    for opp in closed_opps:
        if not opp.is_won or opp.close_date is None:
            continue
        kind = classify_opp_kind(opp.type)
        month = opp.close_date.month
        bucket = out.setdefault(
            month, {"new_biz": 0.0, "expansion": 0.0, "other": 0.0}
        )
        bucket[kind] += opp.acv or 0.0
    return out


# ---------------------------------------------------------------- monthly opps created

def monthly_opps_created(
    all_opps: list[OppRecord],
) -> dict[tuple[int, int], int]:
    """Count of opps created per (year, month). Uses `CreatedDate`.

    Keyed by (year, month) tuple because `all_opps_snapshot` spans the
    trailing 12 calendar months plus open pipeline — collapsing across years
    would silently merge 2025 January counts with 2026 January counts.
    """
    out: dict[tuple[int, int], int] = {}
    for opp in all_opps:
        if opp.created_date is None:
            continue
        key = (opp.created_date.year, opp.created_date.month)
        out[key] = out.get(key, 0) + 1
    return out


# ---------------------------------------------------------------- stage distribution

def stage_distribution(
    all_opps: list[OppRecord],
) -> dict[str, dict[str, Any]]:
    """Open-pipeline distribution across stages — count, ACV, % of pipeline.

    Only open opps contribute (closed excluded). Unknown/missing stages
    bucket under `(unknown)`.
    """
    open_opps = [o for o in all_opps if not o.is_closed]
    total_acv = sum((o.acv or 0.0) for o in open_opps)
    out: dict[str, dict[str, Any]] = {}
    for opp in open_opps:
        stage = opp.stage or "(unknown)"
        bucket = out.setdefault(stage, {"count": 0, "acv": 0.0})
        bucket["count"] += 1
        bucket["acv"] += opp.acv or 0.0
    for bucket in out.values():
        bucket["pct_of_pipeline"] = (
            (bucket["acv"] / total_acv) if total_acv else 0.0
        )
    return out


# ---------------------------------------------------------------- quarterly × segment

def quarterly_closed_by_segment(
    closed_opps: list[OppRecord],
) -> dict[str, dict[str, float]]:
    """Per-segment closed counts + ACV for the quarter.

    Segment comes from `Segment__c`; when missing, falls back to ACV-band
    inference via `pipeline.config.segment_for_acv`. Unclassifiable rows
    bucket under `Unassigned`. Won / lost are tracked separately so the
    sheet can compute a win rate without a second scan.
    """
    out: dict[str, dict[str, float]] = {}
    for opp in closed_opps:
        seg = opp.segment or segment_for_acv(opp.acv) or "Unassigned"
        bucket = out.setdefault(
            seg,
            {"won_count": 0, "won_acv": 0.0, "lost_count": 0, "lost_acv": 0.0},
        )
        amt = opp.acv or 0.0
        if opp.is_won:
            bucket["won_count"] += 1
            bucket["won_acv"] += amt
        else:
            bucket["lost_count"] += 1
            bucket["lost_acv"] += amt
    return out


# ---------------------------------------------------------------- lead source

def lead_source_summary(
    all_opps: list[OppRecord],
) -> list[dict[str, Any]]:
    """Per-lead-source totals, win count, and win rate. Sorted by count desc.

    "Win rate" is wins / total count (including still-open opps) — a
    pipeline-health metric, not a close-rate. Callers that want close-rate
    should filter to closed first.
    """
    agg: dict[str, dict[str, float]] = {}
    for opp in all_opps:
        src = opp.lead_source or "(unknown)"
        bucket = agg.setdefault(src, {"count": 0, "won": 0, "won_acv": 0.0})
        bucket["count"] += 1
        if opp.is_closed and opp.is_won:
            bucket["won"] += 1
            bucket["won_acv"] += opp.acv or 0.0

    out: list[dict[str, Any]] = []
    for src, b in agg.items():
        count = int(b["count"])
        won = int(b["won"])
        out.append({
            "source": src,
            "count": count,
            "won": won,
            "won_acv": float(b["won_acv"]),
            "win_rate": (won / count) if count else 0.0,
        })
    out.sort(key=lambda r: r["count"], reverse=True)
    return out
