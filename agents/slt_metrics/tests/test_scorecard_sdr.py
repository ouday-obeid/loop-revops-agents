"""SDR scorecard builder."""
from __future__ import annotations

from datetime import date

import pytest

from agents.slt_metrics.scorecards import sdr
from agents.slt_metrics.scorecards.quota import RepConfig
from agents.slt_metrics.scorecards.sdr import NooksActivity, SdrMeeting
from agents.slt_metrics.types import OppRecord


def _opp(*, opp_id: str, owner: str | None, acv: float | None) -> OppRecord:
    return OppRecord(
        id=opp_id, name=f"Opp {opp_id}",
        account_id=None, account_name=None, account_website=None, account_type=None,
        owner_id=None, owner_name=owner, owner_role=None, owner_manager=None,
        stage="Demo", is_closed=False, is_won=False,
        amount=acv, acv=acv, fixed_arr=None,
        locations=None, type=None, lead_source=None,
        close_date=None, created_date=None, last_activity_date=None,
        last_modified_date=None, last_stage_change_date=None,
        days_since_stage_change=None, time_in_stage=None,
        probability_sf=None, description=None, next_steps=None, next_step_date=None,
        icp_score=None, segment=None,
    )


def _sdr(name: str) -> RepConfig:
    return RepConfig(
        owner_name=name, role="SDR", team="MM",
        quarterly_quota=None, annual_quota=None,
        attainment_floor_pct=0.70, active=True,
    )


# ------------------------------------------------------------------ meetings

def test_meetings_set_and_held_counts():
    meetings = [
        SdrMeeting(sdr_name="Ada", held=True),
        SdrMeeting(sdr_name="Ada", held=True),
        SdrMeeting(sdr_name="Ada", held=False),
    ]
    cards = sdr.build_sdr_cards(
        meetings=meetings, sourced_opps=[], advanced_opp_ids=[],
        rep_configs=[_sdr("Ada")],
    )
    assert cards[0].meetings_set == 3
    assert cards[0].meetings_held == 2


def test_meetings_held_zero_when_all_no_show():
    meetings = [
        SdrMeeting(sdr_name="Bo", held=False),
        SdrMeeting(sdr_name="Bo", held=False),
    ]
    cards = sdr.build_sdr_cards(
        meetings=meetings, sourced_opps=[], advanced_opp_ids=[],
        rep_configs=[_sdr("Bo")],
    )
    assert cards[0].meetings_set == 2
    assert cards[0].meetings_held == 0


# ------------------------------------------------------------------ pipeline sourced / advanced

def test_pipeline_sourced_sums_sourced_opp_acv():
    sourced = [
        _opp(opp_id="O1", owner="Cam", acv=50_000.0),
        _opp(opp_id="O2", owner="Cam", acv=30_000.0),
    ]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=sourced, advanced_opp_ids=[],
        rep_configs=[_sdr("Cam")],
    )
    assert cards[0].pipeline_sourced == pytest.approx(80_000.0)
    assert cards[0].pipeline_advanced == 0.0


def test_pipeline_advanced_intersects_advanced_ids():
    sourced = [
        _opp(opp_id="O1", owner="Dee", acv=100_000.0),
        _opp(opp_id="O2", owner="Dee", acv=40_000.0),
        _opp(opp_id="O3", owner="Dee", acv=20_000.0),
    ]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=sourced, advanced_opp_ids=["O1", "O3"],
        rep_configs=[_sdr("Dee")],
    )
    assert cards[0].pipeline_sourced == pytest.approx(160_000.0)
    assert cards[0].pipeline_advanced == pytest.approx(120_000.0)


def test_pipeline_handles_null_acv():
    sourced = [
        _opp(opp_id="O1", owner="Eli", acv=None),
        _opp(opp_id="O2", owner="Eli", acv=50_000.0),
    ]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=sourced, advanced_opp_ids=["O1"],
        rep_configs=[_sdr("Eli")],
    )
    assert cards[0].pipeline_sourced == pytest.approx(50_000.0)
    assert cards[0].pipeline_advanced == 0.0


# ------------------------------------------------------------------ leaderboard

def test_leaderboard_ranks_by_pipeline_sourced_descending():
    meetings: list[SdrMeeting] = []
    sourced = [
        _opp(opp_id="O1", owner="Ada", acv=200_000.0),
        _opp(opp_id="O2", owner="Bo",  acv=100_000.0),
        _opp(opp_id="O3", owner="Cam", acv=300_000.0),
    ]
    cards = sdr.build_sdr_cards(
        meetings=meetings, sourced_opps=sourced, advanced_opp_ids=[],
        rep_configs=[_sdr("Ada"), _sdr("Bo"), _sdr("Cam")],
    )
    ranks = {c.sdr_name: c.leaderboard_rank for c in cards}
    assert ranks == {"Cam": 1, "Ada": 2, "Bo": 3}
    # Returned order reflects ranking.
    assert [c.sdr_name for c in cards] == ["Cam", "Ada", "Bo"]


def test_leaderboard_tiebreaks_by_meetings_held():
    sourced = [
        _opp(opp_id="O1", owner="Ada", acv=100_000.0),
        _opp(opp_id="O2", owner="Bo",  acv=100_000.0),
    ]
    meetings = [
        SdrMeeting(sdr_name="Bo", held=True),
        SdrMeeting(sdr_name="Bo", held=True),
        SdrMeeting(sdr_name="Ada", held=True),
    ]
    cards = sdr.build_sdr_cards(
        meetings=meetings, sourced_opps=sourced, advanced_opp_ids=[],
        rep_configs=[_sdr("Ada"), _sdr("Bo")],
    )
    assert cards[0].sdr_name == "Bo"
    assert cards[0].leaderboard_rank == 1
    assert cards[1].sdr_name == "Ada"
    assert cards[1].leaderboard_rank == 2


def test_leaderboard_tiebreaks_by_name_when_all_else_equal():
    sourced = [
        _opp(opp_id="O1", owner="Zed",   acv=50_000.0),
        _opp(opp_id="O2", owner="Alice", acv=50_000.0),
    ]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=sourced, advanced_opp_ids=[],
        rep_configs=[_sdr("Zed"), _sdr("Alice")],
    )
    assert [c.sdr_name for c in cards] == ["Alice", "Zed"]


# ------------------------------------------------------------------ roster handling

def test_sdr_in_roster_with_no_activity_still_emits_card():
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=[], advanced_opp_ids=[],
        rep_configs=[_sdr("Quiet SDR")],
    )
    assert len(cards) == 1
    c = cards[0]
    assert c.sdr_name == "Quiet SDR"
    assert c.meetings_set == 0
    assert c.meetings_held == 0
    assert c.pipeline_sourced == 0.0
    assert c.pipeline_advanced == 0.0
    assert c.leaderboard_rank == 1


def test_sdr_only_in_activity_is_surfaced_as_unknown():
    # Not in rep_configs but shows up in meetings — we still want visibility.
    cards = sdr.build_sdr_cards(
        meetings=[SdrMeeting(sdr_name="Drive-By", held=True)],
        sourced_opps=[], advanced_opp_ids=[],
        rep_configs=[],
    )
    assert len(cards) == 1
    assert cards[0].sdr_name == "Drive-By"
    assert cards[0].meetings_set == 1


def test_non_sdr_rep_configs_are_ignored():
    ae_role = RepConfig(
        owner_name="AE Only", role="AE", team="MM",
        quarterly_quota=300_000.0, annual_quota=None,
        attainment_floor_pct=0.70, active=True,
    )
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=[], advanced_opp_ids=[],
        rep_configs=[ae_role, _sdr("Real SDR")],
    )
    names = {c.sdr_name for c in cards}
    assert names == {"Real SDR"}


# ------------------------------------------------------------------ Nooks

def test_nooks_dials_and_connects_merged_when_present():
    nooks = [NooksActivity(sdr_name="Nia", dials=42, connects=10)]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=[], advanced_opp_ids=[],
        nooks=nooks, rep_configs=[_sdr("Nia")],
    )
    assert cards[0].dials == 42
    assert cards[0].connects == 10


def test_nooks_absent_sdr_gets_none_values():
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=[], advanced_opp_ids=[],
        nooks=None, rep_configs=[_sdr("Nia")],
    )
    assert cards[0].dials is None
    assert cards[0].connects is None


def test_nooks_partial_coverage():
    nooks = [NooksActivity(sdr_name="Ada", dials=60, connects=12)]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=[], advanced_opp_ids=[],
        nooks=nooks, rep_configs=[_sdr("Ada"), _sdr("Bo")],
    )
    by_name = {c.sdr_name: c for c in cards}
    assert by_name["Ada"].dials == 60
    assert by_name["Bo"].dials is None


# ------------------------------------------------------------------ email slug

def test_email_placeholder_slugs_sdr_name():
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=[], advanced_opp_ids=[],
        rep_configs=[_sdr("Sofia O'Brien")],
    )
    assert cards[0].sdr_email == "sofia.obrien@tryloop.ai"


def test_null_owner_on_sourced_opp_is_ignored():
    sourced = [
        _opp(opp_id="O1", owner=None, acv=50_000.0),
        _opp(opp_id="O2", owner="Ada", acv=25_000.0),
    ]
    cards = sdr.build_sdr_cards(
        meetings=[], sourced_opps=sourced, advanced_opp_ids=[],
        rep_configs=[_sdr("Ada")],
    )
    assert len(cards) == 1
    assert cards[0].pipeline_sourced == pytest.approx(25_000.0)
