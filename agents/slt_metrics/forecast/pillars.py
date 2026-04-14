"""5-pillar forecast scorer — D5 covers ICP, Stage, Activity.

Each pillar:
  - Accepts an OppRecord (+ `today` where time-dependent).
  - Returns a `PillarScore(value in [0,1], detail: str)` so the Deal Details
    sheet can surface *why* each pillar got its value.
  - Has no side effects and no DB calls — a pillar is a pure function of the
    opp record, making the backtest replay script trivial to wire.

Timeline and Call pillars land in D6 alongside `scorer.score_deal` which
composes the 5 values against ForecastWeights.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

from agents.slt_metrics.pipeline.config import (
    ACTIVITY_BANDS,
    ACTIVITY_SCORE_MAX,
    ICP_PROXY_CAP,
    LATE_PHASE_STAGES,
    RECENT_TOUCH_BONUS,
    RECENT_TOUCH_THRESHOLD_DAYS,
    SILENCE_PENALTY,
    SILENCE_PENALTY_THRESHOLD_DAYS,
    STAGE_SCORES,
    STAGE_SCORE_MAX,
    TIMELINE_FAR_LOW_SCORE,
    TIMELINE_MID_HIGH_SCORE,
    TIMELINE_MID_LOW_SCORE,
    TIMELINE_MID_WINDOW_DAYS,
    TIMELINE_NEAR_FLOOR,
    TIMELINE_NEAR_INVALID_PENALTY,
    TIMELINE_NEAR_VALID_STAGES,
    TIMELINE_PAST_DUE_SCORE,
    TIMELINE_STAGE_STALL_DAYS,
    TIMELINE_STAGE_STALL_PENALTY,
)
from agents.slt_metrics.types import OppRecord, PillarScore


# ------------------------------------------------------------------ ICP pillar

_PROXY_INBOUND_SOURCES: frozenset[str] = frozenset({"Inbound", "Referral"})


def icp(opp: OppRecord) -> PillarScore:
    """Return the ICP pillar score.

    Priority order:
      1. `ICP_Score__c` on the Opportunity (normalized to 0–1 if given as %)
      2. Proxy: locations + acv + segment + lead source — capped at
         `ICP_PROXY_CAP` so a half-confident proxy can't outvote a real signal
         on a different deal.
    """
    raw = opp.icp_score
    if raw is not None:
        normalized = _normalize_icp(raw)
        return PillarScore(value=normalized, detail=f"sf-icp-score={normalized:.2f}")

    proxy = _icp_proxy(opp)
    capped = min(proxy, ICP_PROXY_CAP)
    detail = f"proxy-capped ({proxy:.2f}→{capped:.2f})" if proxy > capped else f"proxy={proxy:.2f}"
    return PillarScore(value=capped, detail=detail)


def _normalize_icp(raw: float) -> float:
    """ICP_Score__c may arrive as 0–1, 0–10, or 0–100 depending on import.

    Observable: LUCID sometimes writes the 0–100 form; the Loop Pulse nightly
    writes 0–1. Both are valid; we collapse to 0–1 so the pillar weight
    contract holds. Values outside 0–100 get clamped.
    """
    if raw <= 1.0:
        return max(0.0, min(1.0, raw))
    if raw <= 10.0:
        return max(0.0, min(1.0, raw / 10.0))
    return max(0.0, min(1.0, raw / 100.0))


def _icp_proxy(opp: OppRecord) -> float:
    """Weighted proxy: locations (30%), ACV (30%), segment=ENT (20%), inbound (20%).

    Intentionally coarse — the point is a 0–0.5 signal when SF has no native
    score, not to replicate the full ToF model.
    """
    locations = opp.locations or 0
    acv = opp.acv or 0.0
    segment_ent = 1.0 if (opp.segment or "").upper() == "ENT" else 0.0
    inbound = 1.0 if (opp.lead_source or "") in _PROXY_INBOUND_SOURCES else 0.0

    score = (
        0.3 * min(locations / 50.0, 1.0)
        + 0.3 * min(acv / 150_000.0, 1.0)
        + 0.2 * segment_ent
        + 0.2 * inbound
    )
    return max(0.0, min(1.0, score))


# ------------------------------------------------------------------ Stage pillar

def stage(opp: OppRecord) -> PillarScore:
    """Map StageName → 0–1 via LUCID STAGE_SCORES / 25."""
    raw = STAGE_SCORES.get(opp.stage)
    if raw is None:
        # Unknown stage (SF renamed, config drifted): return 0 and surface in detail.
        return PillarScore(value=0.0, detail=f"unknown-stage={opp.stage!r}")
    return PillarScore(
        value=raw / STAGE_SCORE_MAX,
        detail=f"{opp.stage}={raw}/{STAGE_SCORE_MAX}",
    )


# ------------------------------------------------------------------ Activity pillar

def activity(opp: OppRecord, *, today: date) -> PillarScore:
    """Engagement pillar — piecewise days-since-activity bands, +5 recent bonus,
    −10 silence penalty (late-phase only). Final value clamped to 0–1.
    """
    last = opp.last_activity_date
    if last is None:
        return PillarScore(value=0.0, detail="no-activity")

    days = (today - last).days
    if days < 0:
        # SF sometimes reports dates in the future (timezone gymnastics). Treat
        # as "today" for scoring purposes.
        days = 0

    base = _activity_band_score(days)
    bonus = RECENT_TOUCH_BONUS if days <= RECENT_TOUCH_THRESHOLD_DAYS else 0
    penalty = (
        SILENCE_PENALTY
        if (
            days > SILENCE_PENALTY_THRESHOLD_DAYS
            and opp.stage in LATE_PHASE_STAGES
        )
        else 0
    )
    raw = base + bonus + penalty
    raw = max(0, min(ACTIVITY_SCORE_MAX, raw))
    detail = f"{days}d base={base}"
    if bonus:
        detail += f" +{bonus}recent"
    if penalty:
        detail += f" {penalty}silence"
    return PillarScore(value=raw / ACTIVITY_SCORE_MAX, detail=detail)


def _activity_band_score(days: int) -> int:
    for upper, score in ACTIVITY_BANDS:
        if upper is None or days <= upper:
            return score
    # Unreachable — the last band has upper=None — but keeps mypy happy.
    return 0


# ------------------------------------------------------------------ Timeline pillar

_NEAR_HORIZON_DAYS = 30  # close_date within 30d is "near"


def timeline(opp: OppRecord, *, today: date) -> PillarScore:
    """Timeline realism pillar.

    Close-date horizon bands (days-until-close):
      - missing close_date          → 0.0 (we can't evaluate)
      - past-due (<0 days)          → TIMELINE_PAST_DUE_SCORE (0.2)
      - near (0–30 days)            → 1.0 when stage ∈ TIMELINE_NEAR_VALID_STAGES;
                                       else max(1.0 − TIMELINE_NEAR_INVALID_PENALTY,
                                                TIMELINE_NEAR_FLOOR)  (= 0.6 → floored 0.2)
      - mid (31–TIMELINE_MID_WINDOW) → linear ramp TIMELINE_MID_HIGH_SCORE → TIMELINE_MID_LOW_SCORE
      - far (> TIMELINE_MID_WINDOW) → TIMELINE_FAR_LOW_SCORE (0.4)

    Late-phase stage stall: if Time_in_Stage__c > TIMELINE_STAGE_STALL_DAYS
    and stage is Late, subtract TIMELINE_STAGE_STALL_PENALTY. The matching
    STAGE_MISMATCH risk flag is raised in `forecast.risk_flags` (D7) — this
    pillar only scores; flag taxonomy lives downstream.
    """
    if opp.close_date is None:
        return PillarScore(value=0.0, detail="no-close-date")

    days = (opp.close_date - today).days

    if days < 0:
        base = TIMELINE_PAST_DUE_SCORE
        detail = f"past-due({days}d)"
    elif days <= _NEAR_HORIZON_DAYS:
        if opp.stage in TIMELINE_NEAR_VALID_STAGES:
            base = TIMELINE_MID_HIGH_SCORE
            detail = f"near({days}d)/{opp.stage}"
        else:
            base = max(
                TIMELINE_MID_HIGH_SCORE - TIMELINE_NEAR_INVALID_PENALTY,
                TIMELINE_NEAR_FLOOR,
            )
            detail = f"near({days}d)/{opp.stage}-early"
    elif days <= TIMELINE_MID_WINDOW_DAYS:
        # Linear ramp from HIGH at 31d → LOW at TIMELINE_MID_WINDOW_DAYS.
        span = TIMELINE_MID_WINDOW_DAYS - _NEAR_HORIZON_DAYS
        progress = (days - _NEAR_HORIZON_DAYS) / span
        base = TIMELINE_MID_HIGH_SCORE - progress * (
            TIMELINE_MID_HIGH_SCORE - TIMELINE_MID_LOW_SCORE
        )
        detail = f"mid({days}d)={base:.2f}"
    else:
        base = TIMELINE_FAR_LOW_SCORE
        detail = f"far({days}d)"

    penalty = 0.0
    if (
        opp.time_in_stage is not None
        and opp.time_in_stage > TIMELINE_STAGE_STALL_DAYS
        and opp.stage in LATE_PHASE_STAGES
    ):
        penalty = TIMELINE_STAGE_STALL_PENALTY
        detail += f" stall{opp.time_in_stage}d(-{penalty})"

    raw = max(0.0, min(1.0, base - penalty))
    return PillarScore(value=raw, detail=detail)


# ------------------------------------------------------------------ Call pillar (D7 fills in)

def call(opp: OppRecord, *, today: date) -> PillarScore:
    """Call Intel pillar — STUB until D7 wires Fireflies + Haiku classifier.

    Returns 0.0 so D6's scorer composes cleanly; the call weight (0.15 seed)
    contributes a uniform 0 across every deal. The D10 backtest picks up the
    missing signal and we tune weights accordingly once the real call scorer
    lands. The detail string makes the provenance visible on Deal Details.
    """
    # `opp` / `today` unused by the stub — kept in the signature so the D7
    # implementation is a drop-in replacement, not an API change.
    del opp, today
    return PillarScore(value=0.0, detail="call-stub-until-d7")


# ------------------------------------------------------------------ helpers (exported for scorer)

def all_pillars() -> Iterable[str]:
    """Canonical pillar order — matches ForecastWeights field order."""
    return ("icp", "stage", "activity", "timeline", "call")
