"""ICP backtest against O's closed-won sample.

Closed-won accounts should mostly land in tier A or B. If the median falls
below tier-B territory, weights are wrong — fix them before shipping.

Fixture is populated by O on D1 (see tests/fixtures/closed_won_sample.json).
Until then, this test self-skips via the `closed_won_sample` fixture.

Calibration note (2026-04-13, n=30 real SF closed-won Opps):
  Loop's actual close pattern is dominated by `single_brand_franchisee` with
  5–80 locations — the ICP mid-market B-tier band (45–69). Tier A (70+) is
  reserved for `franchise_group_multi_brand` ENT targets. So the median floor
  is set at 50, i.e. a comfortable margin above the tier-B boundary (45), not
  at the A/B midpoint. The A/B-ratio gate (70%) catches distribution drift;
  the median gate catches tier-slump into C/D.
"""
from __future__ import annotations

import statistics

import pytest

from agents.top_of_funnel.icp_scorer import score_account


MIN_TIER_AB_RATIO = 0.70  # >=70% of closed-won should be A/B
MIN_MEDIAN_SCORE = 50      # median closed-won score >= 50 (tier-B territory)


def test_backtest_median_and_ab_ratio(closed_won_sample):
    """Closed-won should score well. Otherwise the weights aren't capturing ICP."""
    scores = [score_account(a) for a in closed_won_sample]
    assert scores, "closed_won_sample.json was empty"

    totals = [s.total for s in scores]
    median = statistics.median(totals)
    ab_ratio = sum(1 for s in scores if s.tier in {"A", "B"}) / len(scores)

    assert median >= MIN_MEDIAN_SCORE, (
        f"median closed-won ICP score = {median} (want >= {MIN_MEDIAN_SCORE}). "
        "Recalibrate weights in config/icp_weights.yaml."
    )
    assert ab_ratio >= MIN_TIER_AB_RATIO, (
        f"{ab_ratio:.0%} of closed-won landed in tier A/B (want >= {MIN_TIER_AB_RATIO:.0%}). "
        "Tier thresholds or weights need adjustment."
    )


def test_backtest_reports_dimension_coverage(closed_won_sample, capsys):
    """Emit a coverage report so O can see which dimensions are driving scores.

    Not an assertion — just prints so the fixture's behavior is visible in CI.
    Useful when recalibrating.
    """
    scores = [score_account(a) for a in closed_won_sample]
    dims = ("ownership", "location_count", "brand_vertical", "growth_signals", "tech_stack_fit")
    avgs = {d: statistics.mean(s.signals[d] for s in scores) for d in dims}
    tier_counts: dict[str, int] = {}
    for s in scores:
        tier_counts[s.tier] = tier_counts.get(s.tier, 0) + 1

    print("\n=== ICP backtest — closed-won sample ===")
    print(f"n={len(scores)}  median={statistics.median(s.total for s in scores):.1f}")
    print(f"tier_counts={tier_counts}")
    for d, avg in avgs.items():
        print(f"  avg {d}: {avg:.1f}")

    captured = capsys.readouterr()
    # Assertion is trivial; the output is the value.
    assert "ICP backtest" in captured.out
