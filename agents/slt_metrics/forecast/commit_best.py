"""Forecast rollups — Commit / Best Case / Weighted.

Inputs: `list[ScoredDeal]` produced by `forecast.scorer.score_all`.
Output: a `ForecastRollup` with total + per-owner + per-segment breakdowns.

Rollup rules (scoping doc §Appendix C):
  - commit_amount     = Σ ACV where score ≥ COMMIT_SCORE_THRESHOLD (80)
  - best_case_amount  = Σ ACV where score ≥ BEST_CASE_SCORE_THRESHOLD (50)
  - weighted_amount   = Σ ACV × probability (every deal contributes)
  - deal_count        = number of deals in scope

The by_owner / by_segment dicts share the same inner shape so the Excel
builder can render them with a single template.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from agents.slt_metrics.pipeline.config import (
    BEST_CASE_SCORE_THRESHOLD,
    COMMIT_SCORE_THRESHOLD,
)
from agents.slt_metrics.types import ForecastRollup, ScoredDeal

# Key used for deals missing an owner / segment in the breakdown dicts. Keeps
# the rollup loss-less — a null owner still contributes to the total.
_UNASSIGNED = "unassigned"


def roll_up(
    deals: Iterable[ScoredDeal],
    *,
    horizon_quarter: str,
) -> ForecastRollup:
    """Produce a ForecastRollup from scored deals."""
    total_commit = 0.0
    total_best = 0.0
    total_weighted = 0.0
    count = 0

    by_owner: dict[str, dict[str, float]] = defaultdict(_empty_bucket)
    by_segment: dict[str, dict[str, float]] = defaultdict(_empty_bucket)

    for d in deals:
        count += 1
        acv = d.acv or 0.0
        contributes_commit = d.score >= COMMIT_SCORE_THRESHOLD
        contributes_best = d.score >= BEST_CASE_SCORE_THRESHOLD

        if contributes_commit:
            total_commit += acv
        if contributes_best:
            total_best += acv
        total_weighted += d.weighted_acv

        owner_key = d.owner_name or _UNASSIGNED
        seg_key = d.segment or _UNASSIGNED
        _accumulate(by_owner[owner_key], acv, d.weighted_acv, contributes_commit, contributes_best)
        _accumulate(by_segment[seg_key], acv, d.weighted_acv, contributes_commit, contributes_best)

    return ForecastRollup(
        horizon_quarter=horizon_quarter,
        commit_amount=total_commit,
        best_case_amount=total_best,
        weighted_amount=total_weighted,
        deal_count=count,
        by_owner=dict(by_owner),
        by_segment=dict(by_segment),
    )


# ------------------------------------------------------------------ helpers

def _empty_bucket() -> dict[str, float]:
    return {
        "commit_amount": 0.0,
        "best_case_amount": 0.0,
        "weighted_amount": 0.0,
        "deal_count": 0.0,
    }


def _accumulate(
    bucket: dict[str, float],
    acv: float,
    weighted: float,
    commit: bool,
    best: bool,
) -> None:
    bucket["deal_count"] += 1
    bucket["weighted_amount"] += weighted
    if commit:
        bucket["commit_amount"] += acv
    if best:
        bucket["best_case_amount"] += acv
