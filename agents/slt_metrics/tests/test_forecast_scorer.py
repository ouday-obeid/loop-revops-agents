"""Composite scorer — score_deal composes the 5 pillars, weights, and
categories into a ScoredDeal.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from agents.slt_metrics.forecast import scorer
from agents.slt_metrics.pipeline.config import WEIGHT_SEEDS
from agents.slt_metrics.types import ForecastWeights, OppRecord, PillarScore


TODAY = date(2026, 4, 13)


def _opp(**overrides: Any) -> OppRecord:
    base = dict(
        id="0061xABC",
        name="Test Opp",
        account_id="0011xACC", account_name="Acme Diner", account_website=None, account_type=None,
        owner_id="0051xREP", owner_name="Sofia Chen", owner_role="AE", owner_manager="Nate Lourens",
        stage="Proposal",
        is_closed=False, is_won=False,
        amount=120_000.0, acv=120_000.0, fixed_arr=None,
        locations=40, type="New Business", lead_source="Inbound",
        close_date=date(2026, 5, 13),  # ~30d out
        created_date=None,
        last_activity_date=date(2026, 4, 11),  # 2 days ago
        last_modified_date=None, last_stage_change_date=None,
        days_since_stage_change=None, time_in_stage=10, probability_sf=None,
        description=None, next_steps=None, next_step_date=None,
        icp_score=0.9, segment="MM",
        products={}, contact_roles=[], raw={},
    )
    base.update(overrides)
    return OppRecord(**base)


def test_score_deal_returns_scored_with_all_pillars():
    d = scorer.score_deal(_opp(), WEIGHT_SEEDS, today=TODAY)
    assert set(d.pillars.keys()) == {"icp", "stage", "activity", "timeline", "call"}
    assert 0 <= d.score <= 100
    assert 0.0 <= d.probability <= 1.0
    assert d.category in {"Strong Commit", "Commit", "High Confidence", "Longshot", "Pipe Dream"}
    assert d.weights_version == WEIGHT_SEEDS.version


def test_score_deal_hot_opportunity_hits_strong_commit():
    # Strong proposal, recent activity, fresh, near-term close, ICP=0.9.
    # Expected high score region.
    d = scorer.score_deal(_opp(), WEIGHT_SEEDS, today=TODAY)
    # Breakdown with seeds:
    # icp 0.25*0.9 = 0.225
    # stage 0.30 * (25/25) = 0.30 → Proposal full mark
    # activity 0.15 * (25+5)/25 capped = 0.15 * 1.0 = 0.15
    # timeline 0.15 * 1.0 (near, Proposal) = 0.15
    # call 0.15 * 0.0 = 0
    # total = 0.825 → score 82 → Strong Commit → 0.80 prob
    assert d.score == 82
    assert d.category == "Strong Commit"
    assert d.probability == pytest.approx(0.80)
    assert d.weighted_acv == pytest.approx(120_000.0 * 0.80)


def test_score_deal_cold_opportunity_is_pipe_dream():
    # Stale, no ICP, early-stage, way-out close date.
    cold = _opp(
        icp_score=None,
        stage="New Meeting Set",
        last_activity_date=None,
        close_date=date(2027, 1, 1),
        time_in_stage=5,
        segment="SMB",
        locations=2,
        acv=10_000.0,
        lead_source="Outbound",
    )
    d = scorer.score_deal(cold, WEIGHT_SEEDS, today=TODAY)
    assert d.category in {"Pipe Dream", "Longshot"}


def test_score_deal_respects_call_override():
    override = PillarScore(value=1.0, detail="override")
    d = scorer.score_deal(_opp(), WEIGHT_SEEDS, today=TODAY, call_override=override)
    assert d.pillars["call"].value == 1.0
    # Score should bump by call weight vs the stub path.
    baseline = scorer.score_deal(_opp(), WEIGHT_SEEDS, today=TODAY).score
    assert d.score > baseline


def test_score_deal_carries_raw_oppRecord_pointer():
    opp = _opp()
    d = scorer.score_deal(opp, WEIGHT_SEEDS, today=TODAY)
    assert d.raw is opp


def test_score_all_scores_every_opp():
    opps = [_opp(id=f"0061xAB{i:02d}") for i in range(3)]
    scored = scorer.score_all(opps, WEIGHT_SEEDS, today=TODAY)
    assert [s.opp_id for s in scored] == [o.id for o in opps]


def test_score_all_call_overrides_apply_by_opp_id():
    opps = [_opp(id="A"), _opp(id="B")]
    overrides = {"B": PillarScore(value=1.0, detail="b-override")}
    scored = scorer.score_all(opps, WEIGHT_SEEDS, today=TODAY, call_overrides=overrides)
    by_id = {s.opp_id: s for s in scored}
    assert by_id["A"].pillars["call"].value == 0.0
    assert by_id["B"].pillars["call"].value == 1.0


def test_score_deal_clamps_if_weights_sum_above_one():
    # Defensive: composing sanity guard — if a test accidentally uses a
    # non-conforming weight set, score stays in [0,100]. (save_weights would
    # reject this, but the scorer itself shouldn't blow up.)
    bogus = ForecastWeights(icp=1.0, stage=1.0, activity=1.0, timeline=1.0, call=1.0)
    d = scorer.score_deal(_opp(), bogus, today=TODAY)
    assert 0 <= d.score <= 100


def test_score_deal_weighted_acv_zero_when_acv_null():
    d = scorer.score_deal(_opp(acv=None), WEIGHT_SEEDS, today=TODAY)
    assert d.weighted_acv == 0.0


def test_score_deal_pillar_details_populated():
    d = scorer.score_deal(_opp(), WEIGHT_SEEDS, today=TODAY)
    for name, p in d.pillars.items():
        assert p.detail, f"pillar {name} missing detail string"
