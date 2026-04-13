"""Pure-data config shared across pipeline / forecast / scorecards / excel.

No I/O, no imports from `shared.mcp.*` or the DB layer. Every consumer should
read values from here so stage/segment/product definitions stay in one place
— porting LUCID `forecast_scorer` was only safe because LUCID kept its
coefficients in one module; we match that discipline.

All values ported 2026-04-13 from:
  LUCID repo: backend/app/services/forecast_scorer.py
  Repo doc:   docs/LOOP_AI_REVOPS_AGENT_TEAM_SCOPE.md
"""
from __future__ import annotations

from typing import Final

from agents.slt_metrics.types import ForecastWeights


# ------------------------------------------------------------------ pipeline stages

# Canonical Loop opp stages. Strings must match `StageName` values in SF exactly
# — when SF renames a stage, update this list and the downstream maps together.
STAGES: Final[tuple[str, ...]] = (
    "No Show",
    "Disqualified",
    "Closed Lost",
    "New Meeting Set",
    "Demo",
    "Business Case",
    "Pilot",
    "Proposal",
    "Closed Won",
)

# Integer ordering used by mover detection. Terminal-negative stages get
# negative ranks so `rank(after) < rank(before)` cleanly flags slippage.
STAGE_RANK: Final[dict[str, int]] = {
    "Closed Lost": -3,
    "Disqualified": -2,
    "No Show": -1,
    "New Meeting Set": 1,
    "Demo": 2,
    "Business Case": 3,
    "Pilot": 4,
    "Proposal": 5,
    "Closed Won": 6,
}

# Phase grouping — used by the forecast/pillars timeline check and the Excel
# builder's Pipeline by Segment sheet.
STAGE_TO_PHASE: Final[dict[str, str]] = {
    "No Show": "early",
    "Disqualified": "early",
    "Closed Lost": "terminal",
    "New Meeting Set": "early",
    "Demo": "early",
    "Business Case": "mid",
    "Pilot": "mid",
    "Proposal": "late",
    "Closed Won": "terminal",
}

LATE_PHASE_STAGES: Final[frozenset[str]] = frozenset(
    s for s, phase in STAGE_TO_PHASE.items() if phase == "late"
)

# Raw 0–25 score per stage, ported from LUCID forecast_scorer `STAGE_SCORES`.
# Divided by 25 inside `forecast.pillars.stage()` to get the 0–1 pillar value.
STAGE_SCORES: Final[dict[str, int]] = {
    "No Show": 0,
    "Disqualified": 0,
    "Closed Lost": 0,
    "New Meeting Set": 5,
    "Demo": 10,
    "Business Case": 20,
    "Pilot": 23,
    "Proposal": 25,
    "Closed Won": 25,
}

STAGE_SCORE_MAX: Final[int] = 25  # denominator for 0–1 normalization


# ------------------------------------------------------------------ segments / coverage

# ACV bands (USD). Upper bound is exclusive; None means open-ended.
SEGMENT_BANDS: Final[dict[str, tuple[float, float | None]]] = {
    "SMB": (0.0, 25_000.0),
    "MM": (25_000.0, 150_000.0),
    "ENT": (150_000.0, None),
}

# Pipeline coverage targets used by `board_metrics.pipeline_coverage`.
# MM = 3x quota, ENT = 4x quota (scoping doc §Appendix C).
COVERAGE_TARGETS: Final[dict[str, float]] = {
    "SMB": 3.0,
    "MM": 3.0,
    "ENT": 4.0,
}


def segment_for_acv(acv: float | None) -> str | None:
    """Infer segment from ACV when `Segment__c` is missing. Returns None for null ACV."""
    if acv is None:
        return None
    for seg, (lo, hi) in SEGMENT_BANDS.items():
        if acv >= lo and (hi is None or acv < hi):
            return seg
    return None


# ------------------------------------------------------------------ products

# SF field name → canonical product name. Drives both the Deal Details sheet
# column header and the `NO_PRODUCTS` risk flag.
PRODUCT_FIELDS: Final[dict[str, str]] = {
    "Count_Balance__c": "Balance",
    "Count_Guard__c": "Guard",
    "Count_Recover__c": "Recover",
    "Count_Re_engage__c": "Re-engage",
    "Count_TruROI__c": "TruROI",
    "Count_TruROI_Plus__c": "TruROI+",
    "Count_Base_Insights__c": "Base Insights",
    "Count_Compass__c": "Compass",
    "Count_White_Glove__c": "White Glove",
}


# ------------------------------------------------------------------ forecast weights

# Phase 1 seed — O approves any change via `@oo slt weights set`. Replayable
# via forecast_history.weights_version.
WEIGHT_SEEDS: Final[ForecastWeights] = ForecastWeights()

# ICP proxy cap — when SF's `ICP_Score__c` is null we compute a proxy from
# locations/ACV/segment/lead source and cap the result at this value. Keeps
# a half-confident proxy from drowning out a real signal on a different deal.
ICP_PROXY_CAP: Final[float] = 0.5


# ------------------------------------------------------------------ activity bands

# Days since last activity → 0–25 raw score, ported from LUCID
# `calc_engagement_score`. Divided by 25 inside forecast.pillars.activity().
# Upper bound is EXCLUSIVE so a 7-day gap still earns the 7-day bucket.
ACTIVITY_BANDS: Final[tuple[tuple[int | None, int], ...]] = (
    (7, 25),        # <= 7 days
    (14, 20),       # 8–14
    (21, 15),       # 15–21
    (30, 10),       # 22–30
    (45, 5),        # 31–45
    (60, 2),        # 46–60
    (None, 0),      # > 60
)

RECENT_TOUCH_BONUS: Final[int] = 5          # added when last activity ≤ 3 days
RECENT_TOUCH_THRESHOLD_DAYS: Final[int] = 3

SILENCE_PENALTY: Final[int] = -10           # subtracted when >60d AND stage is Late
SILENCE_PENALTY_THRESHOLD_DAYS: Final[int] = 60

ACTIVITY_SCORE_MAX: Final[int] = 25


# ------------------------------------------------------------------ timeline pillar

# Close-date horizons (days from today) and the resulting timeline pillar score.
# See plan §Forecast scorer — 5 pillar design, item 4.
TIMELINE_PAST_DUE_SCORE: Final[float] = 0.2
TIMELINE_NEAR_VALID_STAGES: Final[frozenset[str]] = frozenset(
    {"Proposal", "Pilot", "Business Case"}
)
TIMELINE_NEAR_INVALID_PENALTY: Final[float] = 0.4      # subtracted, floored at 0.2
TIMELINE_NEAR_FLOOR: Final[float] = 0.2
TIMELINE_MID_WINDOW_DAYS: Final[int] = 90              # 30–90 linear 1.0 → 0.6
TIMELINE_MID_HIGH_SCORE: Final[float] = 1.0
TIMELINE_MID_LOW_SCORE: Final[float] = 0.6
TIMELINE_FAR_LOW_SCORE: Final[float] = 0.4             # > 90 days decays to 0.4
TIMELINE_STAGE_STALL_DAYS: Final[int] = 60             # Time_in_Stage__c trigger for STAGE_MISMATCH
TIMELINE_STAGE_STALL_PENALTY: Final[float] = 0.3       # subtracted when late+stalled


# ------------------------------------------------------------------ categories / bands

# LUCID-compatible probability + category bands. Upper score bound is inclusive
# of the lower band (i.e. 80 → Strong Commit, 79 → Commit).
PROBABILITY_BANDS: Final[tuple[tuple[int, float, str], ...]] = (
    # (min_score, probability, category)
    (80, 0.80, "Strong Commit"),
    (60, 0.50, "Commit"),
    (40, 0.22, "High Confidence"),
    (20, 0.10, "Longshot"),
    (0,  0.03, "Pipe Dream"),
)

COMMIT_SCORE_THRESHOLD: Final[int] = 80     # commit_amount = Σ ACV where score ≥ 80
BEST_CASE_SCORE_THRESHOLD: Final[int] = 50  # best_case = Σ ACV where score ≥ 50


# ------------------------------------------------------------------ call intel

# Keyword hits on Fireflies `summary.keywords` (case-insensitive substring).
CALL_POSITIVE_KEYWORDS: Final[frozenset[str]] = frozenset({
    "pilot", "contract", "signed", "onboarding", "procurement", "security review",
})
CALL_NEGATIVE_KEYWORDS: Final[frozenset[str]] = frozenset({
    "delay", "budget cut", "pause", "pushed",
})
CALL_POSITIVE_MAX_BONUS: Final[float] = 0.4
CALL_CHAMPION_BONUS: Final[float] = 0.3
CALL_CHAMPION_MIN_TRANSCRIPTS: Final[int] = 2
CALL_CHAMPION_WINDOW_DAYS: Final[int] = 14
CALL_ACTION_ITEMS_BONUS: Final[float] = 0.2
CALL_NEGATIVE_PENALTY: Final[float] = 0.3
CALL_CLASSIFIER_TOP_N_BY_ACV: Final[int] = 20  # Haiku only runs on top-20 opps
CALL_CLASSIFIER_AMBIGUOUS_RANGE: Final[tuple[float, float]] = (0.4, 0.6)
CALL_LOOKBACK_DAYS: Final[int] = 14


# ------------------------------------------------------------------ risk flags

RISK_FLAGS: Final[tuple[str, ...]] = (
    "STAGE_MISMATCH",
    "NO_ENGAGEMENT",
    "ENTERPRISE_STALL",
    "ZOMBIE",
    "ORPHANED",
    "ACV_MISSING",
    "NO_PRODUCTS",
    "REP_RISK",
)

ZOMBIE_DAYS: Final[int] = 90                     # No stage change AND no activity in 90d
ENTERPRISE_STALL_DAYS: Final[int] = 45           # ENT segment, Late phase, no activity
NO_ENGAGEMENT_DAYS: Final[int] = 30


# ------------------------------------------------------------------ fetch horizons

# Default SOQL date literals. `fetch_open_opps` accepts overrides.
DEFAULT_FETCH_FROM: Final[str] = "THIS_QUARTER"
DEFAULT_FETCH_TO: Final[str] = "NEXT_QUARTER"
DEFAULT_FETCH_LIMIT: Final[int] = 1000
