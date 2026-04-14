"""AE scorecard builder."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from agents.slt_metrics.scorecards import ae
from agents.slt_metrics.scorecards.quota import RepConfig
from agents.slt_metrics.types import (
    AeCard,
    CallIntelSignal,
    Mover,
    MoverSet,
    OppRecord,
    PillarScore,
    ScoredDeal,
)


TODAY = date(2026, 4, 13)
Q_START = date(2026, 4, 1)
Q_END = date(2026, 6, 30)


def _opp(
    *,
    opp_id: str,
    owner: str | None,
    stage: str,
    acv: float | None,
    close_date: date | None,
    is_closed: bool,
    is_won: bool,
    created: datetime | None = None,
) -> OppRecord:
    return OppRecord(
        id=opp_id,
        name=f"Opp {opp_id}",
        account_id=None,
        account_name=None,
        account_website=None,
        account_type=None,
        owner_id=None,
        owner_name=owner,
        owner_role=None,
        owner_manager=None,
        stage=stage,
        is_closed=is_closed,
        is_won=is_won,
        amount=acv,
        acv=acv,
        fixed_arr=None,
        locations=None,
        type=None,
        lead_source=None,
        close_date=close_date,
        created_date=created,
        last_activity_date=None,
        last_modified_date=None,
        last_stage_change_date=None,
        days_since_stage_change=None,
        time_in_stage=None,
        probability_sf=None,
        description=None,
        next_steps=None,
        next_step_date=None,
        icp_score=None,
        segment=None,
    )


def _scored(
    *,
    opp_id: str,
    owner: str | None,
    score: int,
    acv: float = 100_000.0,
    call_pillar_value: float | None = None,
) -> ScoredDeal:
    pillars: dict[str, PillarScore] = {}
    if call_pillar_value is not None:
        pillars["call"] = PillarScore(value=call_pillar_value, detail="fake")
    return ScoredDeal(
        opp_id=opp_id,
        opp_name=f"Opp {opp_id}",
        owner_name=owner,
        account_name=None,
        segment=None,
        stage="Pilot",
        amount=acv,
        acv=acv,
        close_date=None,
        score=score,
        probability=0.5,
        category="Commit",
        weighted_acv=acv * 0.5,
        pillars=pillars,
        risk_flags=[],
        weights_version="v1-seed",
    )


def _rep(
    owner: str,
    *,
    role: str = "AE",
    quarterly_quota: float | None = 300_000.0,
    attainment_floor_pct: float = 0.70,
) -> RepConfig:
    return RepConfig(
        owner_name=owner,
        role=role,
        team="MM",
        quarterly_quota=quarterly_quota,
        annual_quota=None,
        attainment_floor_pct=attainment_floor_pct,
        active=True,
    )


# ------------------------------------------------------------------ attainment

def test_attainment_uses_won_acv_within_quarter():
    closed = [
        _opp(opp_id="W1", owner="Ada", stage="Closed Won", acv=100_000.0,
             close_date=date(2026, 4, 5), is_closed=True, is_won=True),
        _opp(opp_id="W2", owner="Ada", stage="Closed Won", acv=50_000.0,
             close_date=date(2026, 5, 10), is_closed=True, is_won=True),
        # Prior quarter won — excluded.
        _opp(opp_id="W0", owner="Ada", stage="Closed Won", acv=999_999.0,
             close_date=date(2026, 3, 31), is_closed=True, is_won=True),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[], movers=None, call_signals=None,
        rep_configs=[_rep("Ada", quarterly_quota=300_000.0)],
        today=TODAY, quarter_start=Q_START,
    )
    assert len(cards) == 1
    assert cards[0].attainment_pct == pytest.approx(150_000.0 / 300_000.0)


def test_attainment_none_when_no_quota():
    closed = [
        _opp(opp_id="W1", owner="Bo", stage="Closed Won", acv=100_000.0,
             close_date=date(2026, 4, 5), is_closed=True, is_won=True),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[_scored(opp_id="S1", owner="Bo", score=70)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Bo", quarterly_quota=None)],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].attainment_pct is None


def test_quota_map_override_when_rep_config_quota_missing():
    closed = [
        _opp(opp_id="W1", owner="Cam", stage="Closed Won", acv=60_000.0,
             close_date=date(2026, 4, 5), is_closed=True, is_won=True),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[_scored(opp_id="S1", owner="Cam", score=70)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Cam", quarterly_quota=None)],
        quota_map={"Cam": 120_000.0},
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].attainment_pct == pytest.approx(0.5)


def test_rep_config_quota_beats_quota_map_default():
    cards = ae.build_ae_cards(
        closed_opps=[
            _opp(opp_id="W1", owner="Dee", stage="Closed Won", acv=100_000.0,
                 close_date=date(2026, 4, 5), is_closed=True, is_won=True),
        ],
        scored_deals=[_scored(opp_id="S1", owner="Dee", score=70)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Dee", quarterly_quota=200_000.0)],
        quota_map={"Dee": 500_000.0},
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].attainment_pct == pytest.approx(0.5)


# ------------------------------------------------------------------ close rate / avg

def test_close_rate_counts_won_and_lost_only():
    closed = [
        _opp(opp_id="W1", owner="Eli", stage="Closed Won", acv=50_000.0,
             close_date=date(2026, 3, 1), is_closed=True, is_won=True),
        _opp(opp_id="W2", owner="Eli", stage="Closed Won", acv=50_000.0,
             close_date=date(2026, 3, 15), is_closed=True, is_won=True),
        _opp(opp_id="L1", owner="Eli", stage="Closed Lost", acv=50_000.0,
             close_date=date(2026, 3, 20), is_closed=True, is_won=False),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[_scored(opp_id="S1", owner="Eli", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Eli")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].close_rate_pct == pytest.approx(2 / 3)


def test_avg_cycle_days_mean_over_wins_only():
    closed = [
        _opp(opp_id="W1", owner="Fi", stage="Closed Won", acv=10_000.0,
             close_date=date(2026, 3, 31), is_closed=True, is_won=True,
             created=datetime(2026, 3, 1, 0, 0, 0)),
        _opp(opp_id="W2", owner="Fi", stage="Closed Won", acv=20_000.0,
             close_date=date(2026, 4, 10), is_closed=True, is_won=True,
             created=datetime(2026, 3, 21, 0, 0, 0)),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[_scored(opp_id="S1", owner="Fi", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Fi")],
        today=TODAY, quarter_start=Q_START,
    )
    # 30 days + 20 days -> avg 25.
    assert cards[0].avg_cycle_days == pytest.approx(25.0)


def test_avg_acv_over_wins_only():
    closed = [
        _opp(opp_id="W1", owner="Gus", stage="Closed Won", acv=100_000.0,
             close_date=date(2026, 3, 31), is_closed=True, is_won=True),
        _opp(opp_id="W2", owner="Gus", stage="Closed Won", acv=200_000.0,
             close_date=date(2026, 4, 2), is_closed=True, is_won=True),
        _opp(opp_id="L1", owner="Gus", stage="Closed Lost", acv=999_999.0,
             close_date=date(2026, 4, 2), is_closed=True, is_won=False),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[_scored(opp_id="S1", owner="Gus", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Gus")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].avg_acv == pytest.approx(150_000.0)


# ------------------------------------------------------------------ pipeline_created / advanced

def test_pipeline_created_and_advanced_from_movers():
    movers = MoverSet(
        period_from=date(2026, 4, 6), period_to=date(2026, 4, 13),
        movers=[
            Mover(opp_id="O1", opp_name="Opp 1", owner_name="Hal",
                  kind="new", before={}, after={"acv": 120_000.0}),
            Mover(opp_id="O2", opp_name="Opp 2", owner_name="Hal",
                  kind="new", before={}, after={"acv": 80_000.0}),
            Mover(opp_id="O3", opp_name="Opp 3", owner_name="Hal",
                  kind="advanced", before={"acv": 40_000.0}, after={"acv": 50_000.0}),
            Mover(opp_id="O4", opp_name="Opp 4", owner_name="Ira",
                  kind="new", before={}, after={"acv": 60_000.0}),
        ],
    )
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=[
            _scored(opp_id="S1", owner="Hal", score=50),
            _scored(opp_id="S2", owner="Ira", score=50),
        ],
        movers=movers, call_signals=None,
        rep_configs=[_rep("Hal"), _rep("Ira")],
        today=TODAY, quarter_start=Q_START,
    )
    by_owner = {c.rep_name: c for c in cards}
    assert by_owner["Hal"].pipeline_created == pytest.approx(200_000.0)
    assert by_owner["Hal"].pipeline_advanced == pytest.approx(50_000.0)
    assert by_owner["Ira"].pipeline_created == pytest.approx(60_000.0)
    assert by_owner["Ira"].pipeline_advanced == 0.0


def test_pipeline_created_handles_null_movers():
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=[_scored(opp_id="S1", owner="Jo", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Jo")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].pipeline_created == 0.0
    assert cards[0].pipeline_advanced == 0.0


# ------------------------------------------------------------------ call grade avg

def test_call_grade_prefers_call_intel_signal():
    scored = [
        _scored(opp_id="O1", owner="Kai", score=50,
                call_pillar_value=0.1),  # would average to 10 if used
    ]
    signals = [
        CallIntelSignal(
            opp_id="O1", transcripts_considered=3, keyword_hits=["pilot"],
            champion_present=True, rep_action_items=1, negative_hits=[],
            classifier_verdict=None, score_delta=0.75,
        ),
    ]
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=scored, movers=None, call_signals=signals,
        rep_configs=[_rep("Kai")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].call_grade_avg == pytest.approx(75.0)


def test_call_grade_falls_back_to_pillar_value_when_no_signal():
    scored = [
        _scored(opp_id="O1", owner="Lee", score=50, call_pillar_value=0.6),
        _scored(opp_id="O2", owner="Lee", score=50, call_pillar_value=0.4),
    ]
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=scored, movers=None, call_signals=[],
        rep_configs=[_rep("Lee")],
        today=TODAY, quarter_start=Q_START,
    )
    # (60 + 40) / 2 = 50.
    assert cards[0].call_grade_avg == pytest.approx(50.0)


def test_call_grade_excludes_stub_zeros_from_average():
    # Stub call pillar values (0.0) should not drag the average down.
    scored = [
        _scored(opp_id="O1", owner="Mo", score=50, call_pillar_value=0.0),
        _scored(opp_id="O2", owner="Mo", score=50, call_pillar_value=0.0),
        _scored(opp_id="O3", owner="Mo", score=50, call_pillar_value=0.8),
    ]
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=scored, movers=None, call_signals=[],
        rep_configs=[_rep("Mo")],
        today=TODAY, quarter_start=Q_START,
    )
    # Only the 0.8 counts -> 80.
    assert cards[0].call_grade_avg == pytest.approx(80.0)


def test_call_grade_none_when_no_call_data_at_all():
    scored = [_scored(opp_id="O1", owner="Nia", score=50, call_pillar_value=None)]
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=scored, movers=None, call_signals=None,
        rep_configs=[_rep("Nia")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].call_grade_avg is None


# ------------------------------------------------------------------ deals_open / deals_commit

def test_deals_open_and_commit_counts():
    scored = [
        _scored(opp_id="O1", owner="Ola", score=85),   # commit
        _scored(opp_id="O2", owner="Ola", score=80),   # commit (>= threshold)
        _scored(opp_id="O3", owner="Ola", score=79),   # open but not commit
        _scored(opp_id="O4", owner="Ola", score=20),   # open but not commit
    ]
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=scored, movers=None, call_signals=None,
        rep_configs=[_rep("Ola")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].deals_open == 4
    assert cards[0].deals_commit == 2


# ------------------------------------------------------------------ rep_perf_score composite

def test_rep_perf_score_composite_uses_available_parts():
    # attainment 1.0 + close_rate 0.5 + call 80 → weighted sum:
    # (0.5*1.0 + 0.3*0.5 + 0.2*0.8) / (0.5+0.3+0.2) = 0.81 → 81
    closed = [
        _opp(opp_id="W1", owner="Pat", stage="Closed Won", acv=300_000.0,
             close_date=date(2026, 4, 5), is_closed=True, is_won=True),
        _opp(opp_id="L1", owner="Pat", stage="Closed Lost", acv=100_000.0,
             close_date=date(2026, 3, 20), is_closed=True, is_won=False),
    ]
    scored = [_scored(opp_id="O1", owner="Pat", score=85, call_pillar_value=0.8)]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=scored, movers=None, call_signals=None,
        rep_configs=[_rep("Pat", quarterly_quota=300_000.0)],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].rep_perf_score == 81


def test_rep_perf_score_none_when_no_signals():
    # No quota, no closed, no call — nothing to score.
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=[_scored(opp_id="O1", owner="Q", score=50, call_pillar_value=None)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Q", quarterly_quota=None)],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].rep_perf_score is None


def test_rep_perf_score_caps_attainment_at_one():
    # Attainment 2.0 must not blow past 100.
    closed = [
        _opp(opp_id="W1", owner="Rio", stage="Closed Won", acv=600_000.0,
             close_date=date(2026, 4, 5), is_closed=True, is_won=True),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[_scored(opp_id="O1", owner="Rio", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Rio", quarterly_quota=300_000.0)],
        today=TODAY, quarter_start=Q_START,
    )
    # Only attainment signal (no lost → close_rate is 1.0 since wins=1,lost=0).
    # weighted = (0.5*1.0 + 0.3*1.0) / 0.8 = 1.0 → 100.
    assert cards[0].rep_perf_score == 100


# ------------------------------------------------------------------ owner shape

def test_owner_in_scored_but_not_rep_configs_still_emits_card():
    scored = [_scored(opp_id="O1", owner="Unknown", score=60, acv=50_000.0)]
    cards = ae.build_ae_cards(
        closed_opps=[], scored_deals=scored, movers=None, call_signals=None,
        rep_configs=[],  # empty roster
        today=TODAY, quarter_start=Q_START,
    )
    assert len(cards) == 1
    assert cards[0].rep_name == "Unknown"
    assert cards[0].attainment_pct is None
    assert cards[0].deals_open == 1


def test_closed_only_history_for_off_roster_rep_is_skipped():
    closed = [
        _opp(opp_id="W1", owner="Retired", stage="Closed Won", acv=100_000.0,
             close_date=date(2026, 3, 10), is_closed=True, is_won=True),
    ]
    cards = ae.build_ae_cards(
        closed_opps=closed, scored_deals=[], movers=None, call_signals=None,
        rep_configs=[],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards == []


def test_only_ae_role_rep_configs_are_considered():
    cards = ae.build_ae_cards(
        closed_opps=[],
        scored_deals=[_scored(opp_id="O1", owner="Sdr One", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Sdr One", role="SDR")],
        today=TODAY, quarter_start=Q_START,
    )
    # Sdr One appears in scored_deals so still emitted, but has no AE quota.
    assert cards[0].attainment_pct is None


def test_email_placeholder_slugs_owner_name():
    cards = ae.build_ae_cards(
        closed_opps=[],
        scored_deals=[_scored(opp_id="O1", owner="Sofia Chen", score=50)],
        movers=None, call_signals=None,
        rep_configs=[_rep("Sofia Chen")],
        today=TODAY, quarter_start=Q_START,
    )
    assert cards[0].rep_email == "sofia.chen@tryloop.ai"


# ------------------------------------------------------------------ flag_rep_risk_owners

def test_flag_rep_risk_owners_uses_configured_floor():
    cards = [
        AeCard(rep_email="a@x", rep_name="A", attainment_pct=0.50,   # below 0.70 floor
               close_rate_pct=None, avg_cycle_days=None, avg_acv=None,
               pipeline_created=0.0, pipeline_advanced=0.0, call_grade_avg=None,
               rep_perf_score=None, deals_open=0, deals_commit=0),
        AeCard(rep_email="b@x", rep_name="B", attainment_pct=0.85,   # above 0.80 floor
               close_rate_pct=None, avg_cycle_days=None, avg_acv=None,
               pipeline_created=0.0, pipeline_advanced=0.0, call_grade_avg=None,
               rep_perf_score=None, deals_open=0, deals_commit=0),
    ]
    configs = [
        _rep("A", attainment_floor_pct=0.70),
        _rep("B", attainment_floor_pct=0.80),
    ]
    risky = ae.flag_rep_risk_owners(cards, configs)
    assert risky == frozenset({"A"})


def test_flag_rep_risk_owners_skips_unknown_or_null_attainment():
    cards = [
        AeCard(rep_email="c@x", rep_name="C", attainment_pct=None,
               close_rate_pct=None, avg_cycle_days=None, avg_acv=None,
               pipeline_created=0.0, pipeline_advanced=0.0, call_grade_avg=None,
               rep_perf_score=None, deals_open=0, deals_commit=0),
    ]
    risky = ae.flag_rep_risk_owners(cards, [_rep("C")])
    assert risky == frozenset()


def test_flag_rep_risk_owners_uses_default_floor_when_rep_unlisted():
    # Rep present on card but not in rep_configs → fall back to DEFAULT floor 0.70.
    cards = [
        AeCard(rep_email="d@x", rep_name="D", attainment_pct=0.65,
               close_rate_pct=None, avg_cycle_days=None, avg_acv=None,
               pipeline_created=0.0, pipeline_advanced=0.0, call_grade_avg=None,
               rep_perf_score=None, deals_open=0, deals_commit=0),
    ]
    risky = ae.flag_rep_risk_owners(cards, [])
    assert risky == frozenset({"D"})
