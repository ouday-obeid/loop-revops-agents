"""Invariants on pipeline.config — the module is pure data, so tests assert
the shapes downstream modules rely on (weight sum, stage monotonicity, band
coverage) rather than re-validating individual constants.
"""
from __future__ import annotations

import pytest

from agents.slt_metrics.pipeline import config
from agents.slt_metrics.types import ForecastWeights


def test_weight_seeds_sum_to_one():
    w: ForecastWeights = config.WEIGHT_SEEDS
    total = w.icp + w.stage + w.activity + w.timeline + w.call
    assert total == pytest.approx(1.0, abs=1e-9), f"weights sum to {total}"


def test_stage_rank_covers_every_stage():
    # Every canonical stage must have a rank; mover detector indexes blindly.
    assert set(config.STAGES) == set(config.STAGE_RANK.keys())


def test_stage_rank_monotonic_progression():
    # Early → Mid → Late → Won should be strictly increasing. Terminal-negative
    # stages sit below 0.
    progression = [
        "New Meeting Set", "Demo", "Business Case", "Pilot", "Proposal", "Closed Won",
    ]
    ranks = [config.STAGE_RANK[s] for s in progression]
    assert ranks == sorted(ranks), "positive ladder must increase"
    assert all(config.STAGE_RANK[s] < 0 for s in ("Closed Lost", "Disqualified", "No Show"))


def test_stage_to_phase_covers_every_stage():
    assert set(config.STAGE_TO_PHASE.keys()) == set(config.STAGES)


def test_late_phase_stages_derived_from_phase_map():
    expected = {s for s, p in config.STAGE_TO_PHASE.items() if p == "late"}
    assert config.LATE_PHASE_STAGES == expected
    assert "Proposal" in config.LATE_PHASE_STAGES


def test_stage_scores_normalize_to_unit_interval():
    for stage, raw in config.STAGE_SCORES.items():
        assert 0 <= raw <= config.STAGE_SCORE_MAX, f"{stage}={raw} outside [0,25]"
    assert config.STAGE_SCORES["Proposal"] == config.STAGE_SCORE_MAX
    assert config.STAGE_SCORES["Closed Won"] == config.STAGE_SCORE_MAX
    assert config.STAGE_SCORES["No Show"] == 0


def test_segment_for_acv_bands():
    assert config.segment_for_acv(None) is None
    assert config.segment_for_acv(0) == "SMB"
    assert config.segment_for_acv(24_999) == "SMB"
    # Lower bound inclusive, upper bound exclusive
    assert config.segment_for_acv(25_000) == "MM"
    assert config.segment_for_acv(149_999.99) == "MM"
    assert config.segment_for_acv(150_000) == "ENT"
    assert config.segment_for_acv(10_000_000) == "ENT"


def test_coverage_targets_present_for_all_segments():
    assert set(config.COVERAGE_TARGETS.keys()) == set(config.SEGMENT_BANDS.keys())
    assert config.COVERAGE_TARGETS["ENT"] > config.COVERAGE_TARGETS["MM"]


def test_product_fields_canonical_names_unique():
    names = list(config.PRODUCT_FIELDS.values())
    assert len(names) == len(set(names)), "duplicate canonical product name"


def test_activity_bands_cover_every_gap():
    # Last tuple must be the open-ended catch-all.
    assert config.ACTIVITY_BANDS[-1][0] is None
    # Days thresholds strictly increasing up to the catch-all.
    days = [d for d, _ in config.ACTIVITY_BANDS if d is not None]
    assert days == sorted(days)
    # Scores monotonic non-increasing.
    scores = [s for _, s in config.ACTIVITY_BANDS]
    assert scores == sorted(scores, reverse=True)


def test_timeline_constants_consistent():
    # Near-invalid penalty cannot push score below floor.
    assert config.TIMELINE_NEAR_INVALID_PENALTY > 0
    assert config.TIMELINE_MID_HIGH_SCORE > config.TIMELINE_MID_LOW_SCORE
    assert config.TIMELINE_MID_LOW_SCORE > config.TIMELINE_FAR_LOW_SCORE
    assert config.TIMELINE_PAST_DUE_SCORE == config.TIMELINE_NEAR_FLOOR


def test_probability_bands_descending_and_cover_zero():
    bands = config.PROBABILITY_BANDS
    mins = [b[0] for b in bands]
    probs = [b[1] for b in bands]
    assert mins == sorted(mins, reverse=True)
    assert probs == sorted(probs, reverse=True)
    # Lowest band must start at 0 so every score lands in some category.
    assert bands[-1][0] == 0


def test_commit_threshold_above_best_case_threshold():
    assert config.COMMIT_SCORE_THRESHOLD > config.BEST_CASE_SCORE_THRESHOLD


def test_call_keywords_disjoint():
    overlap = config.CALL_POSITIVE_KEYWORDS & config.CALL_NEGATIVE_KEYWORDS
    assert not overlap, f"keyword collision: {overlap}"


def test_risk_flags_unique():
    assert len(config.RISK_FLAGS) == len(set(config.RISK_FLAGS))


def test_fetch_defaults_are_soql_literals():
    # These go straight into a SOQL WHERE clause; any space-containing value
    # would break the query.
    assert " " not in config.DEFAULT_FETCH_FROM
    assert " " not in config.DEFAULT_FETCH_TO
    assert config.DEFAULT_FETCH_LIMIT >= 100
