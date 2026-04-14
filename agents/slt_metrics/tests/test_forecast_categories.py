"""Score → probability / category band lookup."""
from __future__ import annotations

import pytest

from agents.slt_metrics.forecast import categories


@pytest.mark.parametrize(
    "score,expected_category,expected_prob",
    [
        (100, "Strong Commit", 0.80),
        (85, "Strong Commit", 0.80),
        (80, "Strong Commit", 0.80),
        (79, "Commit", 0.50),
        (60, "Commit", 0.50),
        (59, "High Confidence", 0.22),
        (40, "High Confidence", 0.22),
        (39, "Longshot", 0.10),
        (20, "Longshot", 0.10),
        (19, "Pipe Dream", 0.03),
        (0, "Pipe Dream", 0.03),
    ],
)
def test_score_to_category_and_probability(score, expected_category, expected_prob):
    assert categories.score_to_category(score) == expected_category
    assert categories.score_to_probability(score) == pytest.approx(expected_prob)


def test_score_to_band_returns_triple():
    band = categories.score_to_band(75)
    assert band == (60, 0.50, "Commit")


def test_score_to_probability_clamps_out_of_range():
    assert categories.score_to_probability(-10) == 0.03
    assert categories.score_to_probability(500) == 0.80


def test_score_to_category_clamps_out_of_range():
    assert categories.score_to_category(-1) == "Pipe Dream"
    assert categories.score_to_category(999) == "Strong Commit"


def test_score_to_probability_accepts_float_rounding():
    # int() coerces via the conversion path; pass something unusual to make sure
    # we don't blow up on float-like input.
    assert categories.score_to_probability(60.9) == 0.50
