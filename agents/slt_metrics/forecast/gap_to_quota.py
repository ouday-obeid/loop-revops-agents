"""Per-AE / team gap-to-quota.

Inputs:
  - `ForecastRollup` with `by_owner` breakdown
  - `quotas`: mapping of owner_name → quarterly quota (USD)
  - `quarter_elapsed_pct`: 0.0–1.0 fraction of the quarter that's elapsed.
    Caller supplies this from the briefing-time clock so this module stays
    free of date math.

Outputs: a `QuotaReport` with per-owner `OwnerGap` entries + team totals.

Flagging rule (scoping doc §Appendix C — "Deal movers untracked"):
  An AE is `at_risk` when gap_pct > `flag_threshold` (default 30%) AND the
  quarter is past `at_risk_after_elapsed` (default 40%). Before that point
  variance is expected; after it, the gap calls for an intervention.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from agents.slt_metrics.types import ForecastRollup


DEFAULT_FLAG_THRESHOLD_PCT: float = 0.30
DEFAULT_AT_RISK_AFTER_ELAPSED: float = 0.40


@dataclass
class OwnerGap:
    owner_name: str
    quota: float
    commit_amount: float
    best_case_amount: float
    weighted_amount: float
    deal_count: int
    gap: float                     # quota − weighted  (positive = short)
    gap_pct: float                 # gap / quota  (1.0 if quota == 0 and gap > 0)
    at_risk: bool


@dataclass
class QuotaReport:
    horizon_quarter: str
    quarter_elapsed_pct: float
    flag_threshold_pct: float
    at_risk_after_elapsed: float
    total_quota: float
    total_commit: float
    total_best_case: float
    total_weighted: float
    team_gap: float
    team_gap_pct: float
    team_at_risk: bool
    owners: list[OwnerGap] = field(default_factory=list)

    def at_risk_owners(self) -> list[OwnerGap]:
        return [o for o in self.owners if o.at_risk]


def build_quota_report(
    rollup: ForecastRollup,
    quotas: Mapping[str, float],
    *,
    quarter_elapsed_pct: float,
    flag_threshold_pct: float = DEFAULT_FLAG_THRESHOLD_PCT,
    at_risk_after_elapsed: float = DEFAULT_AT_RISK_AFTER_ELAPSED,
) -> QuotaReport:
    """Compute gap-to-quota per owner + team rollup."""
    quarter_elapsed_pct = max(0.0, min(1.0, quarter_elapsed_pct))
    past_mid_q = quarter_elapsed_pct > at_risk_after_elapsed

    owners: list[OwnerGap] = []
    # Union of owners in the rollup and owners with quotas — a new-hire with a
    # quota and no deals yet still shows up with a full-quota gap; an owner
    # with deals but no quota record shows up with quota=0.
    all_owners = set(rollup.by_owner.keys()) | set(quotas.keys())
    for owner_name in sorted(all_owners):
        bucket = rollup.by_owner.get(owner_name, _empty_bucket())
        quota = float(quotas.get(owner_name, 0.0))
        gap = _gap_amount(quota, bucket["weighted_amount"])
        gap_pct = _gap_pct(quota, gap)
        at_risk = past_mid_q and gap_pct > flag_threshold_pct and quota > 0
        owners.append(
            OwnerGap(
                owner_name=owner_name,
                quota=quota,
                commit_amount=bucket["commit_amount"],
                best_case_amount=bucket["best_case_amount"],
                weighted_amount=bucket["weighted_amount"],
                deal_count=int(bucket["deal_count"]),
                gap=gap,
                gap_pct=gap_pct,
                at_risk=at_risk,
            )
        )

    total_quota = float(sum(quotas.values()))
    team_gap = _gap_amount(total_quota, rollup.weighted_amount)
    team_gap_pct = _gap_pct(total_quota, team_gap)
    team_at_risk = past_mid_q and team_gap_pct > flag_threshold_pct and total_quota > 0

    return QuotaReport(
        horizon_quarter=rollup.horizon_quarter,
        quarter_elapsed_pct=quarter_elapsed_pct,
        flag_threshold_pct=flag_threshold_pct,
        at_risk_after_elapsed=at_risk_after_elapsed,
        total_quota=total_quota,
        total_commit=rollup.commit_amount,
        total_best_case=rollup.best_case_amount,
        total_weighted=rollup.weighted_amount,
        team_gap=team_gap,
        team_gap_pct=team_gap_pct,
        team_at_risk=team_at_risk,
        owners=owners,
    )


# ------------------------------------------------------------------ helpers

def _empty_bucket() -> dict[str, float]:
    return {
        "commit_amount": 0.0,
        "best_case_amount": 0.0,
        "weighted_amount": 0.0,
        "deal_count": 0.0,
    }


def _gap_amount(quota: float, weighted: float) -> float:
    return max(0.0, quota - weighted)


def _gap_pct(quota: float, gap: float) -> float:
    if quota <= 0:
        # No quota but gap>0 is nonsensical — treat as "can't evaluate" = 0%.
        # No quota and no gap = 0% (fully attained by default).
        return 0.0
    return gap / quota
