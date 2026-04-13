"""Composite forecast scorer — combines the 5 pillars into a ScoredDeal.

Each pillar returns a `PillarScore(value ∈ [0,1], detail: str)`. The scorer
computes `score01 = Σ wᵢ · pᵢ` using the active ForecastWeights, rounds to a
0–100 int, and looks up probability/category via `forecast.categories`.

No DB calls — the scorer is a pure function of (opp, weights, today) + an
optional call_intel map, so the D10 backtest can replay historical states in
a tight loop without polluting state.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping

from agents.slt_metrics.forecast import pillars
from agents.slt_metrics.forecast.categories import score_to_category, score_to_probability
from agents.slt_metrics.forecast.risk_flags import compute_risk_flags
from agents.slt_metrics.types import (
    ForecastWeights,
    OppRecord,
    PillarScore,
    ScoredDeal,
)


def score_deal(
    opp: OppRecord,
    weights: ForecastWeights,
    *,
    today: date,
    call_override: PillarScore | None = None,
    rep_risk_owners: frozenset[str] = frozenset(),
) -> ScoredDeal:
    """Score a single Opportunity.

    `call_override`: optional real CallIntelSignal-derived PillarScore.
    Until D7 wires Fireflies end-to-end, callers leave this None and the
    call pillar falls back to `pillars.call` (a 0.0 stub).

    `rep_risk_owners`: set of owner_name values flagged by the AE scorecard
    (D8). Threaded through to `compute_risk_flags` so REP_RISK surfaces per
    deal when the rep is already under watch.
    """
    icp = pillars.icp(opp)
    stage = pillars.stage(opp)
    activity = pillars.activity(opp, today=today)
    timeline = pillars.timeline(opp, today=today)
    call = call_override if call_override is not None else pillars.call(opp, today=today)

    score01 = (
        weights.icp * icp.value
        + weights.stage * stage.value
        + weights.activity * activity.value
        + weights.timeline * timeline.value
        + weights.call * call.value
    )
    score01 = max(0.0, min(1.0, score01))
    score = int(round(score01 * 100))
    probability = score_to_probability(score)
    category = score_to_category(score)
    weighted_acv = (opp.acv or 0.0) * probability
    risk_flags = compute_risk_flags(opp, today=today, rep_risk_owners=rep_risk_owners)

    return ScoredDeal(
        opp_id=opp.id,
        opp_name=opp.name,
        owner_name=opp.owner_name,
        account_name=opp.account_name,
        segment=opp.segment,
        stage=opp.stage,
        amount=opp.amount,
        acv=opp.acv,
        close_date=opp.close_date,
        score=score,
        probability=probability,
        category=category,
        weighted_acv=weighted_acv,
        pillars={
            "icp": icp,
            "stage": stage,
            "activity": activity,
            "timeline": timeline,
            "call": call,
        },
        risk_flags=risk_flags,
        weights_version=weights.version,
        raw=opp,
    )


def score_all(
    opps: Iterable[OppRecord],
    weights: ForecastWeights,
    *,
    today: date,
    call_overrides: Mapping[str, PillarScore] | None = None,
    rep_risk_owners: frozenset[str] = frozenset(),
) -> list[ScoredDeal]:
    """Score a batch. `call_overrides` keyed by opp.id when available."""
    overrides = call_overrides or {}
    return [
        score_deal(
            o,
            weights,
            today=today,
            call_override=overrides.get(o.id),
            rep_risk_owners=rep_risk_owners,
        )
        for o in opps
    ]
