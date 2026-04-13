"""Churn-risk V1 scoring — pure functions, no I/O.

`score_account(factors)` returns an integer 0-100 (higher = more risk) plus a
per-factor contribution dict for forensics (persisted to `cs_churn_risk.factors_json`).

V1 weights (sponsor & BQ-usage kept at 0 pending their data sources):

    Vitally health drop                35
    Vitally absolute low               20
    NPS signal                         10
    SF Case spike                      15
    Renewal-approaching-no-conversation 15
    Stagnation (days since activity)    5
    Sponsor departure                   0 (V2)
    Usage drop (BQ signal_log)          0 (V2)
    -----------------------------------
    V1 max achievable                 100

Tier routing — aggressive thresholds per O (2026-04-13), Loop AI churn is high:

    50 ≤ score < 70   informational (log + digest)
    70 ≤ score < 85   alert Blaine, CC #agent-cs-log
    85 ≤ score        alert Blaine AND Jackie, urgent task
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal


NpsCategory = Literal["detractor", "passive", "promoter", "unknown"]
Tier = Literal[0, 50, 70, 85]


# Weights — exported so tests and tier tuners can reference them.
WEIGHTS = {
    "health_drop": 35,
    "health_absolute_low": 20,
    "nps": 10,
    "case_spike": 15,
    "renewal_no_conversation": 15,
    "stagnation": 5,
    "sponsor_departure": 0,
    "bq_usage_drop": 0,
}

TIER_LOG_ONLY = 50
TIER_BLAINE = 70
TIER_JACKIE = 85


@dataclass
class ScoringInputs:
    """Data extracted for one account. All fields optional — unknown = 0 contribution."""

    health_current: float | None = None
    health_prev_30d_avg: float | None = None
    nps_category: NpsCategory = "unknown"
    cases_last_30d: int = 0
    cases_prior_30d: int = 0
    days_until_renewal: int | None = None
    days_since_last_call: int | None = None
    days_since_last_activity: int | None = None
    # V2 stubs — keep signatures stable.
    sponsor_departed: bool = False
    bq_usage_drop_ratio: float = 0.0


@dataclass
class ChurnScore:
    score: int
    tier: Tier
    factors: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        return asdict(self)


def _norm_health_drop(current: float | None, prev_avg: float | None) -> float:
    if current is None or prev_avg is None or prev_avg <= 0:
        return 0.0
    drop_ratio = max(0.0, (prev_avg - current) / prev_avg)
    # Saturate at 0.5 (50% drop is the max contributor).
    return min(1.0, drop_ratio / 0.5)


def _norm_health_absolute(current: float | None) -> float:
    if current is None:
        return 0.0
    # 0 score = full risk, 100 score = zero risk.
    return max(0.0, min(1.0, (100.0 - current) / 100.0))


def _norm_nps(cat: NpsCategory) -> float:
    return {"detractor": 1.0, "passive": 0.4, "promoter": 0.0, "unknown": 0.3}.get(cat, 0.3)


def _norm_case_spike(last_30d: int, prior_30d: int) -> float:
    if prior_30d <= 0:
        # No prior baseline → treat as spike only if there are cases now.
        return 1.0 if last_30d >= 3 else 0.0
    ratio = last_30d / prior_30d
    if ratio <= 1.0:
        return 0.0
    # Linear from ratio 1.0 (0.0) to ratio 2.0 (1.0); saturate above.
    return min(1.0, ratio - 1.0)


def _norm_renewal_no_conv(
    days_until_renewal: int | None, days_since_last_call: int | None
) -> float:
    if days_until_renewal is None or days_until_renewal > 120:
        return 0.0
    if days_since_last_call is None:
        return 1.0
    return 1.0 if days_since_last_call >= 45 else 0.0


def _norm_stagnation(days: int | None) -> float:
    if days is None or days <= 60:
        return 0.0
    if days >= 180:
        return 1.0
    # Linear 60 → 180 mapped to 0 → 1.
    return (days - 60) / 120.0


def score_account(inputs: ScoringInputs) -> ChurnScore:
    """Compute churn risk score + tier for one account."""
    factors = {
        "health_drop": _norm_health_drop(inputs.health_current, inputs.health_prev_30d_avg),
        "health_absolute_low": _norm_health_absolute(inputs.health_current),
        "nps": _norm_nps(inputs.nps_category),
        "case_spike": _norm_case_spike(inputs.cases_last_30d, inputs.cases_prior_30d),
        "renewal_no_conversation": _norm_renewal_no_conv(
            inputs.days_until_renewal, inputs.days_since_last_call
        ),
        "stagnation": _norm_stagnation(inputs.days_since_last_activity),
        "sponsor_departure": 1.0 if inputs.sponsor_departed else 0.0,
        "bq_usage_drop": max(0.0, min(1.0, inputs.bq_usage_drop_ratio)),
    }
    contributions = {k: round(v * WEIGHTS[k], 2) for k, v in factors.items()}
    total = round(sum(contributions.values()))
    total = max(0, min(100, total))
    return ChurnScore(score=total, tier=tier_for(total), factors=factors, contributions=contributions)


def tier_for(score: int) -> Tier:
    if score >= TIER_JACKIE:
        return 85
    if score >= TIER_BLAINE:
        return 70
    if score >= TIER_LOG_ONLY:
        return 50
    return 0


def non_zero_factor_count(factors: dict[str, float]) -> int:
    """Tier ≥70 alerts require ≥2 non-zero factors (anti-false-positive guard)."""
    return sum(1 for v in factors.values() if v > 0)
