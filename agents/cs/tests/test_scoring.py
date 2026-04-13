"""M3 — Pure churn-risk scoring function tests."""
from __future__ import annotations

import pytest

from agents.cs.risk.scoring import (
    ScoringInputs,
    WEIGHTS,
    non_zero_factor_count,
    score_account,
    tier_for,
)


class TestHealthDrop:
    def test_no_drop_no_contribution(self):
        s = score_account(ScoringInputs(health_current=90, health_prev_30d_avg=90))
        assert s.factors["health_drop"] == 0.0

    def test_saturates_at_50pct_drop(self):
        # 50% drop → full contribution
        s = score_account(ScoringInputs(health_current=45, health_prev_30d_avg=90))
        assert s.factors["health_drop"] == 1.0
        assert s.contributions["health_drop"] == WEIGHTS["health_drop"]

    def test_saturates_above_50pct(self):
        s = score_account(ScoringInputs(health_current=10, health_prev_30d_avg=90))
        assert s.factors["health_drop"] == 1.0

    def test_25pct_drop_is_half(self):
        s = score_account(ScoringInputs(health_current=67.5, health_prev_30d_avg=90))
        assert s.factors["health_drop"] == pytest.approx(0.5, abs=0.01)

    def test_missing_prev_returns_zero(self):
        s = score_account(ScoringInputs(health_current=50))
        assert s.factors["health_drop"] == 0.0


class TestAbsoluteLow:
    def test_score_100_is_zero_risk(self):
        s = score_account(ScoringInputs(health_current=100))
        assert s.factors["health_absolute_low"] == 0.0

    def test_score_0_is_full_risk(self):
        s = score_account(ScoringInputs(health_current=0))
        assert s.factors["health_absolute_low"] == 1.0

    def test_score_50_is_half(self):
        s = score_account(ScoringInputs(health_current=50))
        assert s.factors["health_absolute_low"] == 0.5

    def test_missing_is_zero(self):
        s = score_account(ScoringInputs())
        assert s.factors["health_absolute_low"] == 0.0


class TestNps:
    @pytest.mark.parametrize("cat,expected", [
        ("detractor", 1.0),
        ("passive", 0.4),
        ("promoter", 0.0),
        ("unknown", 0.3),
    ])
    def test_categories(self, cat, expected):
        s = score_account(ScoringInputs(nps_category=cat))
        assert s.factors["nps"] == expected


class TestCaseSpike:
    def test_ratio_1_is_zero(self):
        s = score_account(ScoringInputs(cases_last_30d=5, cases_prior_30d=5))
        assert s.factors["case_spike"] == 0.0

    def test_ratio_2_is_full(self):
        s = score_account(ScoringInputs(cases_last_30d=10, cases_prior_30d=5))
        assert s.factors["case_spike"] == 1.0

    def test_ratio_above_2_saturates(self):
        s = score_account(ScoringInputs(cases_last_30d=100, cases_prior_30d=5))
        assert s.factors["case_spike"] == 1.0

    def test_ratio_1_5_is_half(self):
        s = score_account(ScoringInputs(cases_last_30d=9, cases_prior_30d=6))
        assert s.factors["case_spike"] == pytest.approx(0.5, abs=0.01)

    def test_no_prior_with_cases_triggers(self):
        s = score_account(ScoringInputs(cases_last_30d=5, cases_prior_30d=0))
        assert s.factors["case_spike"] == 1.0

    def test_no_prior_with_few_cases_does_not_trigger(self):
        s = score_account(ScoringInputs(cases_last_30d=2, cases_prior_30d=0))
        assert s.factors["case_spike"] == 0.0


class TestRenewalNoConv:
    def test_no_renewal_window_zero(self):
        s = score_account(ScoringInputs(days_until_renewal=None))
        assert s.factors["renewal_no_conversation"] == 0.0

    def test_renewal_past_120_zero(self):
        s = score_account(ScoringInputs(days_until_renewal=180, days_since_last_call=90))
        assert s.factors["renewal_no_conversation"] == 0.0

    def test_renewal_within_120_no_call_triggers(self):
        s = score_account(ScoringInputs(days_until_renewal=90, days_since_last_call=None))
        assert s.factors["renewal_no_conversation"] == 1.0

    def test_renewal_within_120_stale_call_triggers(self):
        s = score_account(ScoringInputs(days_until_renewal=90, days_since_last_call=60))
        assert s.factors["renewal_no_conversation"] == 1.0

    def test_renewal_within_120_recent_call_zero(self):
        s = score_account(ScoringInputs(days_until_renewal=90, days_since_last_call=10))
        assert s.factors["renewal_no_conversation"] == 0.0


class TestStagnation:
    def test_recent_activity_zero(self):
        s = score_account(ScoringInputs(days_since_last_activity=30))
        assert s.factors["stagnation"] == 0.0

    def test_60_days_boundary(self):
        s = score_account(ScoringInputs(days_since_last_activity=60))
        assert s.factors["stagnation"] == 0.0

    def test_180_days_saturates(self):
        s = score_account(ScoringInputs(days_since_last_activity=180))
        assert s.factors["stagnation"] == 1.0

    def test_beyond_180_saturates(self):
        s = score_account(ScoringInputs(days_since_last_activity=365))
        assert s.factors["stagnation"] == 1.0

    def test_120_days_is_half(self):
        s = score_account(ScoringInputs(days_since_last_activity=120))
        assert s.factors["stagnation"] == pytest.approx(0.5, abs=0.01)


class TestTotalScore:
    def test_zero_inputs_score_is_nps_unknown_only(self):
        # Default NPS=unknown contributes 0.3 × 10 = 3 (no other signal).
        s = score_account(ScoringInputs())
        assert s.score == 3
        assert s.tier == 0

    def test_promoter_nps_with_no_other_signal_is_zero(self):
        s = score_account(ScoringInputs(nps_category="promoter"))
        assert s.score == 0
        assert s.tier == 0

    def test_max_achievable_v1_is_100(self):
        s = score_account(
            ScoringInputs(
                health_current=0,
                health_prev_30d_avg=100,
                nps_category="detractor",
                cases_last_30d=100,
                cases_prior_30d=5,
                days_until_renewal=30,
                days_since_last_call=90,
                days_since_last_activity=365,
            )
        )
        assert s.score == 100
        assert s.tier == 85

    def test_v2_stubs_contribute_zero(self):
        s = score_account(
            ScoringInputs(
                nps_category="promoter", sponsor_departed=True, bq_usage_drop_ratio=1.0
            )
        )
        assert s.score == 0  # V1 weights are 0 for both V2 factors
        assert s.contributions["sponsor_departure"] == 0
        assert s.contributions["bq_usage_drop"] == 0


class TestTiers:
    @pytest.mark.parametrize("score,tier", [
        (0, 0), (49, 0),
        (50, 50), (69, 50),
        (70, 70), (84, 70),
        (85, 85), (100, 85),
    ])
    def test_tier_boundaries(self, score, tier):
        assert tier_for(score) == tier


class TestNonZeroFactors:
    def test_counts_positive_factors(self):
        factors = {"a": 0.0, "b": 0.1, "c": 1.0, "d": 0.0}
        assert non_zero_factor_count(factors) == 2

    def test_all_zero(self):
        assert non_zero_factor_count({"a": 0.0, "b": 0.0}) == 0
