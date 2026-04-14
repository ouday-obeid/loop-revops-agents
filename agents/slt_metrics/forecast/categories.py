"""Score → probability / category bands.

Pure lookup logic — no dependencies on an OppRecord or Fireflies. The bands
are defined in `pipeline.config.PROBABILITY_BANDS`; this module is the
single place that decides "is a score 65 a Commit or High Confidence?" so
the scorer, the Excel builder, and the backtest all agree.

Ported from LUCID forecast_scorer lines 84–107. Bands are in descending
`min_score` order — the first band whose `min_score` the score clears is
the match.
"""
from __future__ import annotations

from agents.slt_metrics.pipeline.config import PROBABILITY_BANDS


def score_to_probability(score: int) -> float:
    """Return the probability for a 0–100 score. Out-of-range → clamped."""
    s = max(0, min(100, int(score)))
    for min_score, prob, _ in PROBABILITY_BANDS:
        if s >= min_score:
            return prob
    return PROBABILITY_BANDS[-1][1]


def score_to_category(score: int) -> str:
    """Return the named category for a 0–100 score."""
    s = max(0, min(100, int(score)))
    for min_score, _, name in PROBABILITY_BANDS:
        if s >= min_score:
            return name
    return PROBABILITY_BANDS[-1][2]


def score_to_band(score: int) -> tuple[int, float, str]:
    """Return the full (min_score, probability, category) tuple. Convenience
    for callers that need all three (e.g., the Deal Details sheet renders the
    band boundary alongside the category).
    """
    s = max(0, min(100, int(score)))
    for band in PROBABILITY_BANDS:
        if s >= band[0]:
            return band
    return PROBABILITY_BANDS[-1]
