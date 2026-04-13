"""AE (Account Executive) scorecard builder.

`build_ae_cards(...)` consumes already-computed inputs — closed opps in the
lookback window, today's scored open pipeline, mover diffs, call signals,
rep_config quotas — and emits one `AeCard` per AE. Keeps this module purely
compositional so the Excel sheet, briefings, and the weekly Slack reply all
share the same numbers.

Data columns (scoping doc §Appendix C):
  - attainment_pct     = Σ won ACV in quarter / quarterly_quota
  - close_rate_pct     = won / (won + lost) over lookback
  - avg_cycle_days     = mean(close_date − created_date) over won
  - avg_acv            = mean(acv) over won
  - pipeline_created   = Σ ACV of "new" movers attributed to this AE
  - pipeline_advanced  = Σ ACV of "advanced" movers attributed to this AE
  - call_grade_avg     = mean(call pillar score * 100) for the AE's open deals
  - rep_perf_score     = composite 0–100, NOT a forecast weight
  - deals_open         = count of open scored deals
  - deals_commit       = count of scored deals with score >= COMMIT_SCORE_THRESHOLD
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from agents.slt_metrics.pipeline.config import COMMIT_SCORE_THRESHOLD
from agents.slt_metrics.scorecards.quota import DEFAULT_ATTAINMENT_FLOOR_PCT, RepConfig
from agents.slt_metrics.types import (
    AeCard,
    CallIntelSignal,
    MoverSet,
    OppRecord,
    ScoredDeal,
)

log = logging.getLogger(__name__)

# Composite rep_perf_score weights. Deliberately simple — tune once we have
# real quarter-over-quarter attainment history.
_PERF_WEIGHT_ATTAINMENT = 0.50
_PERF_WEIGHT_CLOSE_RATE = 0.30
_PERF_WEIGHT_CALL_GRADE = 0.20


def build_ae_cards(
    *,
    closed_opps: Iterable[OppRecord],
    scored_deals: Iterable[ScoredDeal],
    movers: MoverSet | None,
    call_signals: Iterable[CallIntelSignal] | None,
    rep_configs: Iterable[RepConfig],
    today: date,
    quarter_start: date,
    quota_map: Mapping[str, float] | None = None,
) -> list[AeCard]:
    """Return one AeCard per AE in rep_configs. Owners absent from rep_configs
    but present in scored_deals are emitted with quota=None / attainment=None.
    """
    rep_list = [r for r in rep_configs if (r.role or "").upper() == "AE"]
    config_by_owner = {r.owner_name: r for r in rep_list}
    quotas = dict(quota_map or {})
    for r in rep_list:
        # rep_config quota wins over any caller-supplied default.
        if r.quarterly_quota is not None:
            quotas[r.owner_name] = r.quarterly_quota

    closed_by_owner: dict[str, list[OppRecord]] = {}
    for o in closed_opps:
        if o.owner_name:
            closed_by_owner.setdefault(o.owner_name, []).append(o)

    scored_by_owner: dict[str, list[ScoredDeal]] = {}
    for d in scored_deals:
        if d.owner_name:
            scored_by_owner.setdefault(d.owner_name, []).append(d)

    movers_created = _movers_by_kind(movers, "new")
    movers_advanced = _movers_by_kind(movers, "advanced")

    call_by_opp: dict[str, CallIntelSignal] = {
        s.opp_id: s for s in (call_signals or [])
    }

    all_owners = (
        set(config_by_owner.keys())
        | set(closed_by_owner.keys())
        | set(scored_by_owner.keys())
    )

    cards: list[AeCard] = []
    for owner in sorted(all_owners):
        rep = config_by_owner.get(owner)
        if rep is None and not scored_by_owner.get(owner):
            # Closed-only history for an inactive rep — skip emitting a card.
            continue
        closed = closed_by_owner.get(owner, [])
        won = [o for o in closed if o.is_won]
        lost = [o for o in closed if o.is_closed and not o.is_won]
        scored = scored_by_owner.get(owner, [])

        quota = quotas.get(owner)
        won_in_quarter = sum(
            (o.acv or 0.0)
            for o in won
            if o.close_date is not None and o.close_date >= quarter_start
        )
        attainment_pct = _safe_ratio(won_in_quarter, quota) if quota else None

        close_rate_pct = _safe_ratio(len(won), len(won) + len(lost))
        avg_cycle_days = _avg_cycle_days(won)
        avg_acv = _avg(o.acv for o in won)
        pipeline_created = _sum_movers(movers_created.get(owner))
        pipeline_advanced = _sum_movers(movers_advanced.get(owner))
        call_grade_avg = _avg_call_grade(scored, call_by_opp)
        deals_commit = sum(1 for d in scored if d.score >= COMMIT_SCORE_THRESHOLD)
        rep_perf_score = _composite_perf(attainment_pct, close_rate_pct, call_grade_avg)

        cards.append(
            AeCard(
                rep_email=_rep_email_placeholder(owner),
                rep_name=owner,
                attainment_pct=attainment_pct,
                close_rate_pct=close_rate_pct,
                avg_cycle_days=avg_cycle_days,
                avg_acv=avg_acv,
                pipeline_created=pipeline_created,
                pipeline_advanced=pipeline_advanced,
                call_grade_avg=call_grade_avg,
                rep_perf_score=rep_perf_score,
                deals_open=len(scored),
                deals_commit=deals_commit,
            )
        )
    return cards


def flag_rep_risk_owners(
    cards: Iterable[AeCard],
    rep_configs: Iterable[RepConfig],
) -> frozenset[str]:
    """Return owner_names whose attainment is below their configured floor.

    Feeds `forecast.risk_flags.compute_risk_flags(..., rep_risk_owners=…)` so
    the REP_RISK flag surfaces on every deal an at-risk rep owns.
    """
    floors = {
        r.owner_name: (r.attainment_floor_pct or DEFAULT_ATTAINMENT_FLOOR_PCT)
        for r in rep_configs
    }
    risky: set[str] = set()
    for c in cards:
        if c.rep_name is None or c.attainment_pct is None:
            continue
        floor = floors.get(c.rep_name, DEFAULT_ATTAINMENT_FLOOR_PCT)
        if c.attainment_pct < floor:
            risky.add(c.rep_name)
    return frozenset(risky)


# ------------------------------------------------------------------ helpers

def _movers_by_kind(movers: MoverSet | None, kind: str) -> dict[str, list[Any]]:
    if movers is None:
        return {}
    grouped: dict[str, list[Any]] = {}
    for m in movers.movers:
        if m.kind != kind or not m.owner_name:
            continue
        grouped.setdefault(m.owner_name, []).append(m)
    return grouped


def _sum_movers(movers: list[Any] | None) -> float:
    if not movers:
        return 0.0
    total = 0.0
    for m in movers:
        after = m.after or {}
        acv = after.get("acv") if isinstance(after, dict) else None
        if acv is None:
            before = m.before or {}
            acv = before.get("acv") if isinstance(before, dict) else None
        total += float(acv or 0.0)
    return total


def _safe_ratio(numerator: float, denominator: float | None) -> float | None:
    if denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _avg(values: Iterable[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return sum(xs) / len(xs)


def _avg_cycle_days(won: list[OppRecord]) -> float | None:
    deltas: list[int] = []
    for o in won:
        if o.close_date is not None and o.created_date is not None:
            created = o.created_date.date() if hasattr(o.created_date, "date") else o.created_date
            deltas.append((o.close_date - created).days)
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def _avg_call_grade(
    scored: list[ScoredDeal],
    call_by_opp: dict[str, CallIntelSignal],
) -> float | None:
    grades: list[float] = []
    for d in scored:
        # Prefer the CallIntelSignal score (0–1 scale) when present.
        sig = call_by_opp.get(d.opp_id)
        if sig is not None:
            grades.append(sig.score_delta * 100.0)
            continue
        # Fall back to whatever the call pillar resolved to on the ScoredDeal.
        call_pillar = d.pillars.get("call")
        if call_pillar is not None:
            grades.append(call_pillar.value * 100.0)
    if not grades:
        return None
    # Exclude stub zeros — they dilute the average to 0 before D7 wires through.
    nonzero = [g for g in grades if g > 0]
    if nonzero:
        return sum(nonzero) / len(nonzero)
    return 0.0


def _composite_perf(
    attainment_pct: float | None,
    close_rate_pct: float | None,
    call_grade_avg: float | None,
) -> int | None:
    """0–100 data column. None when no signal exists at all."""
    parts: list[tuple[float, float]] = []
    if attainment_pct is not None:
        parts.append((_PERF_WEIGHT_ATTAINMENT, min(1.0, attainment_pct)))
    if close_rate_pct is not None:
        parts.append((_PERF_WEIGHT_CLOSE_RATE, close_rate_pct))
    if call_grade_avg is not None:
        parts.append((_PERF_WEIGHT_CALL_GRADE, min(1.0, call_grade_avg / 100.0)))
    if not parts:
        return None
    weight_total = sum(w for w, _ in parts)
    score01 = sum(w * v for w, v in parts) / weight_total
    return int(round(max(0.0, min(1.0, score01)) * 100))


def _rep_email_placeholder(owner_name: str) -> str:
    """Until we thread owner email through the SOQL, synthesize a stable key."""
    slug = owner_name.lower().replace(" ", ".").replace("'", "")
    return f"{slug}@tryloop.ai"
