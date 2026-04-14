"""Risk flag taxonomy — 8-flag inventory per scoping doc §Appendix C.

Each flag is an independent condition — a single opp can trip many. The
Deal Details sheet renders them as a comma-separated column; briefings
pick the top-N most severe per AE.

Ported from LUCID forecast_scorer lines 234–282, extended to cover the
two Loop-only flags (`ORPHANED`, `REP_RISK`). Thresholds live in
`pipeline.config` so the backtest can grid-search them without code edits.
"""
from __future__ import annotations

from datetime import date

from agents.slt_metrics.pipeline.config import (
    ENTERPRISE_STALL_DAYS,
    LATE_PHASE_STAGES,
    NO_ENGAGEMENT_DAYS,
    PRODUCT_FIELDS,
    TIMELINE_STAGE_STALL_DAYS,
    ZOMBIE_DAYS,
)
from agents.slt_metrics.types import OppRecord


def compute_risk_flags(
    opp: OppRecord,
    *,
    today: date,
    rep_risk_owners: frozenset[str] = frozenset(),
) -> list[str]:
    """Return the ordered list of RISK_FLAGS triggered for this opp.

    `rep_risk_owners`: set of owner_name values flagged by the AE scorecard
    (attainment below threshold, close-rate crash, etc.). D8 builds this set;
    until then callers pass an empty frozenset.
    """
    flags: list[str] = []

    days_since_activity = _days_between(today, opp.last_activity_date)

    if _stage_mismatch(opp):
        flags.append("STAGE_MISMATCH")

    if days_since_activity is not None and days_since_activity > NO_ENGAGEMENT_DAYS:
        flags.append("NO_ENGAGEMENT")

    if _enterprise_stall(opp, days_since_activity):
        flags.append("ENTERPRISE_STALL")

    if _zombie(opp, days_since_activity, today=today):
        flags.append("ZOMBIE")

    if _orphaned(opp):
        flags.append("ORPHANED")

    if _acv_missing(opp):
        flags.append("ACV_MISSING")

    if _no_products(opp):
        flags.append("NO_PRODUCTS")

    if opp.owner_name and opp.owner_name in rep_risk_owners:
        flags.append("REP_RISK")

    return flags


# ------------------------------------------------------------------ predicates

def _stage_mismatch(opp: OppRecord) -> bool:
    """Stalled in a Late-phase stage beyond TIMELINE_STAGE_STALL_DAYS.

    This mirrors the timeline pillar's stall penalty — the pillar docks the
    score, the flag surfaces the diagnosis so briefings can say *why*.
    """
    return (
        opp.time_in_stage is not None
        and opp.time_in_stage > TIMELINE_STAGE_STALL_DAYS
        and opp.stage in LATE_PHASE_STAGES
    )


def _enterprise_stall(opp: OppRecord, days_since_activity: int | None) -> bool:
    if (opp.segment or "").upper() != "ENT":
        return False
    if opp.stage not in LATE_PHASE_STAGES:
        return False
    return days_since_activity is not None and days_since_activity > ENTERPRISE_STALL_DAYS


def _zombie(opp: OppRecord, days_since_activity: int | None, *, today: date) -> bool:
    if days_since_activity is None or days_since_activity <= ZOMBIE_DAYS:
        return False
    days_since_stage = _days_between(today, opp.last_stage_change_date)
    if days_since_stage is None:
        # Fall back to `days_since_stage_change` if SF supplied it directly.
        days_since_stage = opp.days_since_stage_change
    return days_since_stage is not None and days_since_stage > ZOMBIE_DAYS


def _orphaned(opp: OppRecord) -> bool:
    # No owner, or no named contacts to drive the deal forward.
    no_owner = not (opp.owner_id or opp.owner_name)
    no_contacts = not any(cr.name for cr in opp.contact_roles)
    return no_owner or no_contacts


def _acv_missing(opp: OppRecord) -> bool:
    return opp.acv is None or opp.acv <= 0


def _no_products(opp: OppRecord) -> bool:
    if not opp.products:
        return True
    # `PRODUCT_FIELDS` lists every canonical product — anything outside this set
    # is ignored so a stray custom field doesn't mask a genuinely empty cart.
    known = set(PRODUCT_FIELDS.values())
    return not any(
        (name in known and (count or 0) > 0) for name, count in opp.products.items()
    )


# ------------------------------------------------------------------ helpers

def _days_between(today: date, ref: date | None) -> int | None:
    if ref is None:
        return None
    delta = (today - ref).days
    # Guard against SF's occasional future-dated timestamps.
    return max(0, delta)
