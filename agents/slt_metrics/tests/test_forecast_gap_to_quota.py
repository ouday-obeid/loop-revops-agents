"""Gap-to-quota — per-AE gap + team rollup + at-risk flagging."""
from __future__ import annotations

import pytest

from agents.slt_metrics.forecast import gap_to_quota as g
from agents.slt_metrics.types import ForecastRollup


def _rollup(by_owner: dict[str, dict[str, float]]) -> ForecastRollup:
    total_commit = sum(b["commit_amount"] for b in by_owner.values())
    total_best = sum(b["best_case_amount"] for b in by_owner.values())
    total_weighted = sum(b["weighted_amount"] for b in by_owner.values())
    total_count = int(sum(b["deal_count"] for b in by_owner.values()))
    return ForecastRollup(
        horizon_quarter="FY2026-Q2",
        commit_amount=total_commit,
        best_case_amount=total_best,
        weighted_amount=total_weighted,
        deal_count=total_count,
        by_owner=by_owner,
    )


def _bucket(commit=0.0, best=0.0, weighted=0.0, count=0) -> dict[str, float]:
    return {
        "commit_amount": float(commit),
        "best_case_amount": float(best),
        "weighted_amount": float(weighted),
        "deal_count": float(count),
    }


def test_build_quota_report_happy_path():
    rollup = _rollup({
        "Sofia Chen":  _bucket(commit=200_000.0, best=300_000.0, weighted=220_000.0, count=4),
        "Marcus Lee":  _bucket(commit=100_000.0, best=180_000.0, weighted=140_000.0, count=3),
    })
    quotas = {"Sofia Chen": 300_000.0, "Marcus Lee": 250_000.0}
    report = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.5)

    sofia = next(o for o in report.owners if o.owner_name == "Sofia Chen")
    assert sofia.quota == 300_000.0
    assert sofia.gap == pytest.approx(80_000.0)
    assert sofia.gap_pct == pytest.approx(80_000.0 / 300_000.0)

    assert report.total_quota == 550_000.0
    assert report.total_weighted == 360_000.0
    assert report.team_gap == pytest.approx(190_000.0)
    assert report.team_gap_pct == pytest.approx(190_000.0 / 550_000.0)


def test_flag_threshold_default_thirty_pct():
    # Sofia has a 40% gap — flagged. Marcus has 20% — not flagged.
    rollup = _rollup({
        "Sofia":  _bucket(weighted=60_000.0, count=1),
        "Marcus": _bucket(weighted=80_000.0, count=1),
    })
    quotas = {"Sofia": 100_000.0, "Marcus": 100_000.0}
    report = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.5)
    at_risk_names = {o.owner_name for o in report.at_risk_owners()}
    assert "Sofia" in at_risk_names
    assert "Marcus" not in at_risk_names


def test_not_at_risk_before_mid_quarter():
    # Same 40% gap, but quarter only 20% elapsed → NOT flagged (too early).
    rollup = _rollup({
        "Sofia":  _bucket(weighted=60_000.0, count=1),
    })
    quotas = {"Sofia": 100_000.0}
    report = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.20)
    assert not report.at_risk_owners()
    assert not report.team_at_risk


def test_owner_with_quota_but_no_deals_shows_full_gap():
    # New hire has a quota but hasn't sourced anything yet.
    rollup = _rollup({})
    quotas = {"Jordan": 200_000.0}
    report = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.6)
    jordan = report.owners[0]
    assert jordan.owner_name == "Jordan"
    assert jordan.quota == 200_000.0
    assert jordan.weighted_amount == 0.0
    assert jordan.gap == 200_000.0
    assert jordan.gap_pct == pytest.approx(1.0)
    assert jordan.at_risk


def test_owner_with_deals_no_quota_is_neutral():
    # Missing quota row — gap_pct is 0.0 so the AE never flags on it alone.
    rollup = _rollup({
        "Erin": _bucket(commit=50_000.0, weighted=40_000.0, count=1),
    })
    report = g.build_quota_report(rollup, quotas={}, quarter_elapsed_pct=0.8)
    erin = report.owners[0]
    assert erin.quota == 0.0
    assert erin.gap == 0.0
    assert erin.gap_pct == 0.0
    assert not erin.at_risk


def test_weighted_exceeds_quota_gap_is_zero():
    rollup = _rollup({
        "Sofia": _bucket(weighted=300_000.0, count=4),
    })
    quotas = {"Sofia": 200_000.0}
    report = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.6)
    sofia = report.owners[0]
    assert sofia.gap == 0.0
    assert sofia.gap_pct == 0.0
    assert not sofia.at_risk


def test_quarter_elapsed_clamped():
    rollup = _rollup({"Sofia": _bucket(weighted=50_000.0, count=1)})
    quotas = {"Sofia": 100_000.0}
    low = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=-0.5)
    high = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=2.0)
    assert low.quarter_elapsed_pct == 0.0
    assert high.quarter_elapsed_pct == 1.0


def test_owners_sorted_alphabetically():
    rollup = _rollup({
        "Zara":  _bucket(weighted=10_000.0, count=1),
        "Adam":  _bucket(weighted=20_000.0, count=1),
        "Marcus": _bucket(weighted=30_000.0, count=1),
    })
    quotas = {"Zara": 100_000.0, "Adam": 100_000.0, "Marcus": 100_000.0}
    report = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.5)
    assert [o.owner_name for o in report.owners] == ["Adam", "Marcus", "Zara"]


def test_team_at_risk_gates_on_elapsed_threshold():
    rollup = _rollup({
        "Sofia": _bucket(weighted=50_000.0, count=1),
    })
    quotas = {"Sofia": 200_000.0}
    # Team gap pct = 75% but too early → not team-at-risk.
    r_early = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.30)
    assert not r_early.team_at_risk
    # Later in the quarter → team at risk.
    r_late = g.build_quota_report(rollup, quotas, quarter_elapsed_pct=0.80)
    assert r_late.team_at_risk
