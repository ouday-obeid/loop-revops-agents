"""Rubric shape, weighted-score math, grade-label thresholds, critical-item cap."""
from __future__ import annotations

import pytest

from agents.sales_reps.call_grader import rubrics


# --------------------------------------------------------------------- shape

def test_gradable_types_match_rubric_keys():
    assert rubrics.GRADABLE_TYPES == frozenset(
        {"first_call", "second_call", "follow_up", "sdr_cold_call"}
    )


def test_non_gradable_filter_covers_all_expected():
    expected = {"onboarding", "cs", "pilot", "renewal", "internal", "headroom", "other"}
    assert rubrics.NON_GRADABLE_TYPES == frozenset(expected)
    # Gradable and non-gradable must be disjoint.
    assert not (rubrics.GRADABLE_TYPES & rubrics.NON_GRADABLE_TYPES)


def test_get_rubric_unknown_raises():
    with pytest.raises(ValueError):
        rubrics.get_rubric("onboarding")


def test_get_rubric_returns_expected_shape():
    r = rubrics.get_rubric("first_call")
    assert r.call_type == "first_call"
    assert r.scorecard_name.startswith("AE Certification")
    assert len(r.sections) == 6
    # Every section has weight > 0
    assert all(s.weight > 0 for s in r.sections)


def test_all_rubrics_have_at_least_one_critical_section():
    for call_type in rubrics.GRADABLE_TYPES:
        r = rubrics.get_rubric(call_type)
        assert any(s.critical_items for s in r.sections), f"{call_type} has no critical items"


def test_is_gradable():
    assert rubrics.is_gradable("first_call")
    assert not rubrics.is_gradable("internal")
    assert not rubrics.is_gradable("xyz")


# --------------------------------------------------------------------- math

def test_max_weighted_matches_sum_of_5x_weights():
    r = rubrics.get_rubric("first_call")
    expected = sum(5 * s.weight for s in r.sections)
    assert r.max_weighted == expected


def test_weighted_score_all_fives_is_hundred_percent():
    r = rubrics.get_rubric("first_call")
    section_scores = {s.name: 5 for s in r.sections}
    out = rubrics.compute_weighted_score(r, section_scores)
    assert out["percentage"] == 100.0
    assert out["grade_label"] == "pass_excellent"


def test_weighted_score_all_ones_is_twenty_percent():
    r = rubrics.get_rubric("first_call")
    section_scores = {s.name: 1 for s in r.sections}
    out = rubrics.compute_weighted_score(r, section_scores)
    assert out["percentage"] == 20.0
    assert out["grade_label"] == "fail_major_gaps"


def test_weighted_score_clamps_out_of_range():
    r = rubrics.get_rubric("sdr_cold_call")
    # Feeding a 99 for one section must clamp to 5, a 0 must clamp to 1.
    section_scores = {s.name: 99 for s in r.sections}
    out = rubrics.compute_weighted_score(r, section_scores)
    assert out["percentage"] == 100.0

    section_scores = {s.name: 0 for s in r.sections}
    out = rubrics.compute_weighted_score(r, section_scores)
    assert out["percentage"] == 20.0  # all clamped to 1


def test_weighted_score_missing_sections_defaulted_to_one():
    r = rubrics.get_rubric("sdr_cold_call")
    out = rubrics.compute_weighted_score(r, {})  # empty
    assert out["percentage"] == 20.0  # every missing section treated as 1


# --------------------------------------------------------------------- thresholds

@pytest.mark.parametrize("pct,label", [
    (70.0, "pass_excellent"),
    (85.0, "pass_excellent"),
    (69.9, "pass_good"),
    (50.0, "pass_good"),
    (49.9, "fail_needs_work"),
    (35.0, "fail_needs_work"),
    (34.9, "fail_major_gaps"),
    (0.0, "fail_major_gaps"),
])
def test_grade_label_thresholds(pct: float, label: str):
    r = rubrics.get_rubric("first_call")
    assert r.grade_label(pct) == label


# --------------------------------------------------------------------- sections

def test_first_call_section_weights_match_outbounder_calibration():
    r = rubrics.get_rubric("first_call")
    by_name = {s.name: s for s in r.sections}
    # Discovery and Demo are heaviest — matches Outbounder's tuning.
    assert by_name["Discovery"].weight == 1.5
    assert by_name["Demo / Platform Walkthrough"].weight == 1.5
    assert by_name["Introduction & Upfront Agenda"].weight == 0.5
    assert by_name["Close & Deal Progression"].weight == 0.5


def test_follow_up_all_weights_equal_one():
    r = rubrics.get_rubric("follow_up")
    assert all(s.weight == 1.0 for s in r.sections)
    assert len(r.sections) == 8


def test_section_names_unique_per_rubric():
    for call_type in rubrics.GRADABLE_TYPES:
        r = rubrics.get_rubric(call_type)
        names = [s.name for s in r.sections]
        assert len(names) == len(set(names)), f"{call_type} has duplicate section names"
