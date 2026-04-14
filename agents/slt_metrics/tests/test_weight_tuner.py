"""Weight tuner — Dirichlet-near-seed sampling + top-K selection."""
from __future__ import annotations

import random
from datetime import date

import pytest

from agents.slt_metrics.forecast import weight_tuner as wt
from agents.slt_metrics.forecast.backtest import BacktestResult
from agents.slt_metrics.forecast.weight_tuner import WeightProposal
from agents.slt_metrics.pipeline.config import WEIGHT_SEEDS
from agents.slt_metrics.types import ForecastWeights


def _result(*, mape: float | None = 0.15, brier: float | None = 0.2) -> BacktestResult:
    return BacktestResult(
        weights_version="x",
        window_start=date(2026, 1, 1), window_end=date(2026, 3, 31),
        step_days=7, cohorts=[],
        overall_mape=mape, brier_score=brier,
        category_hit_rate={}, deal_count=0,
        actuals_total=0.0, weighted_total=0.0,
        commit_total=0.0, best_case_total=0.0,
    )


# ------------------------------------------------------------------ _sample_near

def test_sample_near_returns_weights_summing_to_one():
    rng = random.Random(42)
    for _ in range(50):
        sample = wt._sample_near(
            WEIGHT_SEEDS, max_deviation=0.05, rng=rng,
        )
        total = (sample.icp + sample.stage + sample.activity
                 + sample.timeline + sample.call)
        assert total == pytest.approx(1.0, abs=1e-9)


def test_sample_near_respects_minimum_pillar_weight():
    rng = random.Random(1)
    for _ in range(50):
        sample = wt._sample_near(WEIGHT_SEEDS, max_deviation=0.2, rng=rng)
        for pillar in ("icp", "stage", "activity", "timeline", "call"):
            val = getattr(sample, pillar)
            assert val > 0.0, f"{pillar} went to zero"


def test_sample_near_is_deterministic_given_same_seed():
    s1 = wt._sample_near(WEIGHT_SEEDS, max_deviation=0.05, rng=random.Random(7))
    s2 = wt._sample_near(WEIGHT_SEEDS, max_deviation=0.05, rng=random.Random(7))
    assert s1 == s2


def test_sample_near_stays_close_to_seed_within_deviation():
    rng = random.Random(13)
    # Each pillar-delta pre-normalization is bounded by max_deviation; after
    # renormalization the bound is approximate, but for ±0.05 over seeds in the
    # 0.15–0.30 range it stays well under 0.10.
    for _ in range(30):
        sample = wt._sample_near(WEIGHT_SEEDS, max_deviation=0.05, rng=rng)
        for pillar in ("icp", "stage", "activity", "timeline", "call"):
            delta = abs(getattr(sample, pillar) - getattr(WEIGHT_SEEDS, pillar))
            assert delta < 0.10, f"{pillar} drifted {delta} — too far"


# ------------------------------------------------------------------ composite

def test_composite_score_lower_is_better():
    better = _result(mape=0.05, brier=0.1)
    worse = _result(mape=0.30, brier=0.3)
    assert wt._composite_score(better) < wt._composite_score(worse)


def test_composite_score_none_mape_gets_penalty():
    with_null = _result(mape=None, brier=0.1)
    without = _result(mape=0.5, brier=0.1)
    # Penalty > any realistic MAPE so null-MAPE always ranks worse than finite.
    assert wt._composite_score(with_null) > wt._composite_score(without)


def test_composite_score_both_none_gets_max_penalty():
    s = wt._composite_score(_result(mape=None, brier=None))
    expected = (
        wt.COMPOSITE_MAPE_WEIGHT * wt.COMPOSITE_PENALTY
        + wt.COMPOSITE_BRIER_WEIGHT * wt.COMPOSITE_PENALTY
    )
    assert s == pytest.approx(expected)


# ------------------------------------------------------------------ propose

def test_propose_invokes_backtest_per_sample_plus_seed():
    calls = {"n": 0}

    def fake_backtest(weights: ForecastWeights) -> BacktestResult:
        calls["n"] += 1
        return _result(mape=0.2, brier=0.2)

    wt.propose_weight_tunings(
        seed=WEIGHT_SEEDS, backtest_fn=fake_backtest,
        n_samples=5, top_k=3, rng=random.Random(0),
    )
    assert calls["n"] == 6   # seed + 5 samples


def test_propose_returns_top_k_sorted_ascending():
    scores = iter([0.4, 0.1, 0.3, 0.05, 0.2])

    def fake_backtest(weights: ForecastWeights) -> BacktestResult:
        score = next(scores)
        # Put the MAPE at twice the score so composite = score*0.5 + brier*0.5.
        return _result(mape=score * 2, brier=0.0)

    top = wt.propose_weight_tunings(
        seed=WEIGHT_SEEDS, backtest_fn=fake_backtest,
        n_samples=4, top_k=3, rng=random.Random(0),
    )
    assert len(top) == 3
    assert [p.rank for p in top] == [1, 2, 3]
    assert [p.composite_score for p in top] == sorted(p.composite_score for p in top)


def test_propose_includes_seed_as_a_candidate():
    """The seed version string should appear in the scored pool."""
    versions_seen: list[str] = []

    def fake_backtest(weights: ForecastWeights) -> BacktestResult:
        versions_seen.append(weights.version)
        return _result(mape=0.0, brier=0.0)  # tie — all equal composite

    wt.propose_weight_tunings(
        seed=WEIGHT_SEEDS, backtest_fn=fake_backtest,
        n_samples=3, top_k=5, rng=random.Random(0),
    )
    assert WEIGHT_SEEDS.version in versions_seen


def test_propose_sample_versions_use_tuned_label():
    versions_seen: list[str] = []

    def fake_backtest(weights: ForecastWeights) -> BacktestResult:
        versions_seen.append(weights.version)
        return _result(mape=0.1, brier=0.1)

    wt.propose_weight_tunings(
        seed=WEIGHT_SEEDS, backtest_fn=fake_backtest,
        n_samples=2, top_k=5, label="tuned", rng=random.Random(0),
    )
    sample_versions = [v for v in versions_seen if v != WEIGHT_SEEDS.version]
    assert sample_versions
    assert all("-tuned-" in v for v in sample_versions)


def test_propose_swallows_backtest_failures_and_keeps_survivors():
    def fake_backtest(weights: ForecastWeights) -> BacktestResult:
        if weights.version == WEIGHT_SEEDS.version:
            raise RuntimeError("seed backtest blew up")
        return _result(mape=0.1, brier=0.1)

    top = wt.propose_weight_tunings(
        seed=WEIGHT_SEEDS, backtest_fn=fake_backtest,
        n_samples=3, top_k=3, rng=random.Random(0),
    )
    assert len(top) == 3  # seed dropped, 3 survivors still ranked
    assert all(p.weights.version != WEIGHT_SEEDS.version for p in top)


def test_propose_validates_inputs():
    def ok(weights: ForecastWeights) -> BacktestResult:
        return _result()

    with pytest.raises(ValueError):
        wt.propose_weight_tunings(
            seed=WEIGHT_SEEDS, backtest_fn=ok, n_samples=0,
        )
    with pytest.raises(ValueError):
        wt.propose_weight_tunings(
            seed=WEIGHT_SEEDS, backtest_fn=ok, n_samples=5, top_k=0,
        )
    with pytest.raises(ValueError):
        wt.propose_weight_tunings(
            seed=WEIGHT_SEEDS, backtest_fn=ok, n_samples=5, max_deviation=0.3,
        )
    with pytest.raises(ValueError):
        wt.propose_weight_tunings(
            seed=WEIGHT_SEEDS, backtest_fn=ok, n_samples=5, max_deviation=0.0,
        )


def test_propose_default_seed_uses_weight_seeds():
    """Omitting `seed` should default to the locked Phase-1 seed."""
    versions_seen: list[str] = []

    def fake_backtest(weights: ForecastWeights) -> BacktestResult:
        versions_seen.append(weights.version)
        return _result(mape=0.1, brier=0.1)

    wt.propose_weight_tunings(
        backtest_fn=fake_backtest,
        n_samples=1, top_k=3, rng=random.Random(0),
    )
    assert WEIGHT_SEEDS.version in versions_seen


# ------------------------------------------------------------------ render

def test_render_proposals_markdown_has_header_and_rows():
    proposals = [
        WeightProposal(
            weights=ForecastWeights(version="v2-tuned-2026-04-13"),
            backtest_result=_result(mape=0.12, brier=0.1),
            composite_score=0.11, rank=1,
        ),
        WeightProposal(
            weights=WEIGHT_SEEDS,
            backtest_result=_result(mape=0.15, brier=0.2),
            composite_score=0.175, rank=2,
        ),
    ]
    md = wt.render_proposals_markdown(proposals)
    assert "| Rank |" in md
    assert "v2-tuned-2026-04-13" in md
    assert WEIGHT_SEEDS.version in md
    assert "12.0%" in md or "15.0%" in md   # at least one MAPE formatted


def test_render_proposals_markdown_empty_list_shows_hint():
    md = wt.render_proposals_markdown([])
    assert "No viable proposals" in md
