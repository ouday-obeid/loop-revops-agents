"""Call Intel — Fireflies keyword scoring + Haiku classifier gating."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from agents.slt_metrics.forecast import call_intel
from agents.slt_metrics.pipeline.config import (
    CALL_ACTION_ITEMS_BONUS,
    CALL_CHAMPION_BONUS,
    CALL_NEGATIVE_PENALTY,
    CALL_POSITIVE_KEYWORDS,
    CALL_POSITIVE_MAX_BONUS,
)
from agents.slt_metrics.types import ContactRole, OppRecord


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
        locations=30, type="New Business", lead_source="Inbound",
        close_date=date(2026, 5, 13),
        created_date=None,
        last_activity_date=date(2026, 4, 11),
        last_modified_date=None, last_stage_change_date=None,
        days_since_stage_change=None, time_in_stage=10, probability_sf=None,
        description=None, next_steps=None, next_step_date=None,
        icp_score=0.85, segment="MM",
        products={"Balance": 3}, contact_roles=[
            ContactRole(
                contact_id="C1", name="Buyer Bob", email="bob@acme.com",
                title="VP Ops", role="Economic Buyer", is_primary=True,
            ),
        ],
        raw={},
    )
    base.update(overrides)
    return OppRecord(**base)


def _transcript(
    tid: str,
    *,
    keywords: list[str] | None = None,
    overview: str = "",
    participants: list[str] | None = None,
    action_items: list[str] | None = None,
    date_str: str | None = None,
) -> dict[str, Any]:
    return {
        "id": tid,
        "date": date_str or "2026-04-10T15:00:00Z",
        "participants": participants or ["bob@acme.com", "ae@tryloop.ai"],
        "summary": {
            "keywords": keywords or [],
            "overview": overview,
            "action_items": action_items or [],
        },
    }


def _fetcher(by_email: dict[str, list[dict[str, Any]]]):
    def _fn(email: str, period_from: str, period_to: str) -> list[dict[str, Any]]:
        return by_email.get(email, [])
    return _fn


def test_no_transcripts_returns_zero_score():
    overrides, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY, list_transcripts_fn=_fetcher({}),
    )
    assert overrides["0061xABC"].value == 0.0
    assert overrides["0061xABC"].detail == "no-transcripts"
    assert signals[0].transcripts_considered == 0


def test_positive_keyword_hit_awards_bonus():
    t = _transcript("T1", keywords=["pilot", "contract"])
    overrides, _ = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
    )
    # 2 positive keyword hits — bonus scales distinctly, bounded at MAX.
    score = overrides["0061xABC"].value
    assert score > 0.0
    assert score <= CALL_POSITIVE_MAX_BONUS + CALL_ACTION_ITEMS_BONUS  # no champ/ai


def test_negative_keyword_applies_penalty():
    # Positive + negative simultaneously — penalty applies.
    t = _transcript("T1", keywords=["pilot", "budget cut"])
    overrides, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
    )
    assert "budget cut" in signals[0].negative_hits
    # Score reduced by the penalty.
    assert overrides["0061xABC"].value < CALL_POSITIVE_MAX_BONUS


def test_score_floor_at_zero_when_only_negative():
    t = _transcript("T1", keywords=["delay", "pushed"])
    overrides, _ = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
    )
    assert overrides["0061xABC"].value == 0.0


def test_champion_requires_two_distinct_buyer_emails_in_window():
    # Two transcripts with two distinct buyer-side emails inside 14d.
    opp = _opp(contact_roles=[
        ContactRole("C1", "Bob", "bob@acme.com", "VP", "Buyer", True),
        ContactRole("C2", "Eve", "eve@acme.com", "Dir", "Influencer", False),
    ])
    t1 = _transcript(
        "T1", participants=["bob@acme.com", "ae@tryloop.ai"],
        date_str="2026-04-10T15:00:00Z",
    )
    t2 = _transcript(
        "T2", participants=["eve@acme.com", "ae@tryloop.ai"],
        date_str="2026-04-08T15:00:00Z",
    )
    overrides, signals = call_intel.score_call_intel(
        [opp], today=TODAY,
        list_transcripts_fn=_fetcher({
            "bob@acme.com": [t1], "eve@acme.com": [t2],
        }),
    )
    assert signals[0].champion_present
    assert overrides["0061xABC"].value >= CALL_CHAMPION_BONUS


def test_champion_window_excludes_stale_transcripts():
    # Only one fresh transcript — should fail the ≥2-emails threshold.
    t_old = _transcript(
        "T-old", participants=["bob@acme.com", "eve@acme.com", "ae@tryloop.ai"],
        date_str="2026-02-01T15:00:00Z",  # > 14d ago
    )
    overrides, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t_old]}),
    )
    assert not signals[0].champion_present


def test_rep_action_items_counted_when_loop_domain_present():
    t = _transcript(
        "T1",
        keywords=["pilot"],
        action_items=["ae@tryloop.ai: send pilot contract", "bob@acme.com: review internally"],
    )
    _, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
    )
    # Only the tryloop.ai-owned action item counts.
    assert signals[0].rep_action_items == 1


def test_classifier_runs_only_for_top_n_and_ambiguous_band():
    many = [
        _opp(id=f"OPP_{i:03d}", acv=float(1_000_000 - i))
        for i in range(25)
    ]
    # All 6 positive keywords → positive bonus caps at 0.4 = bottom of ambiguous band.
    t = _transcript("T1", keywords=list(CALL_POSITIVE_KEYWORDS))

    calls_seen: list[str] = []

    def classifier(opp: OppRecord, transcripts: list[dict[str, Any]]) -> dict[str, Any]:
        calls_seen.append(opp.id)
        return {"champion_strength": 0.9, "mutual_plan": 0.9, "decision_authority": 0.9}

    call_intel.score_call_intel(
        many, today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
        classifier_fn=classifier,
    )
    # Top 20 by ACV all sit in the ambiguous band → classifier fires for each.
    # Tail 5 never call the classifier regardless of score.
    assert len(calls_seen) == 20
    ids_called = set(calls_seen)
    top_20 = {f"OPP_{i:03d}" for i in range(20)}
    assert ids_called == top_20
    for tail_id in (f"OPP_{i:03d}" for i in range(20, 25)):
        assert tail_id not in ids_called


def test_classifier_skips_when_keyword_score_outside_ambiguous_band():
    # 1 keyword hit → score ≈ 0.067, below ambiguous band → classifier must not run.
    t = _transcript("T1", keywords=["pilot"])
    calls_seen: list[str] = []

    def classifier(opp: OppRecord, transcripts: list[dict[str, Any]]) -> dict[str, Any]:
        calls_seen.append(opp.id)
        return {}

    call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
        classifier_fn=classifier,
    )
    assert calls_seen == []


def test_classifier_verdict_blended_into_score():
    # Engineer keyword score into the ambiguous band: all 6 positive keywords
    # alone = 0.4, matching the band floor so the classifier fires.
    t = _transcript("T1", keywords=list(CALL_POSITIVE_KEYWORDS))

    def classifier(opp: OppRecord, transcripts: list[dict[str, Any]]) -> dict[str, Any]:
        return {"champion_strength": 1.0, "mutual_plan": 1.0, "decision_authority": 1.0}

    overrides, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
        classifier_fn=classifier,
    )
    # Keyword score 0.4 blended 50/50 with classifier 1.0 = 0.7
    assert overrides["0061xABC"].value == pytest.approx(0.7)
    assert signals[0].classifier_verdict == {
        "champion_strength": 1.0, "mutual_plan": 1.0, "decision_authority": 1.0,
    }


def test_classifier_exception_does_not_crash_pipeline():
    # Score must land in the ambiguous band for the classifier path to execute.
    t = _transcript("T1", keywords=list(CALL_POSITIVE_KEYWORDS))

    def bad_classifier(opp: OppRecord, transcripts: list[dict[str, Any]]) -> dict[str, Any]:
        raise RuntimeError("haiku timeout")

    overrides, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
        classifier_fn=bad_classifier,
    )
    # Pipeline returns a non-None pillar; classifier verdict stays null.
    assert overrides["0061xABC"].value >= 0.0
    assert signals[0].classifier_verdict is None


def test_fireflies_fetcher_exception_treated_as_empty():
    def boom(email: str, period_from: str, period_to: str) -> list[dict[str, Any]]:
        raise RuntimeError("fireflies 500")

    overrides, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY, list_transcripts_fn=boom,
    )
    assert overrides["0061xABC"].value == 0.0
    assert signals[0].transcripts_considered == 0


def test_opp_without_contact_roles_skips_fetch():
    calls: list[str] = []

    def counting(email: str, period_from: str, period_to: str) -> list[dict[str, Any]]:
        calls.append(email)
        return []

    orphaned = _opp(contact_roles=[])
    call_intel.score_call_intel(
        [orphaned], today=TODAY, list_transcripts_fn=counting,
    )
    assert calls == []


def test_detail_string_describes_signals():
    t = _transcript(
        "T1", keywords=["pilot", "contract"],
        action_items=["ae@tryloop.ai: send contract"],
    )
    overrides, _ = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
    )
    detail = overrides["0061xABC"].detail
    assert "1tx" in detail
    assert "contract" in detail and "pilot" in detail
    assert "ai=1" in detail


def test_returned_signal_carries_all_fields():
    t = _transcript("T1", keywords=["pilot", "delay"])
    _, signals = call_intel.score_call_intel(
        [_opp()], today=TODAY,
        list_transcripts_fn=_fetcher({"bob@acme.com": [t]}),
    )
    s = signals[0]
    assert s.opp_id == "0061xABC"
    assert "pilot" in s.keyword_hits
    assert "delay" in s.negative_hits
    assert s.transcripts_considered == 1
