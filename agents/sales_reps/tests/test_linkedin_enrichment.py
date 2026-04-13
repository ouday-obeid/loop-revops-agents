"""Decision-maker enrichment — merge strategy, Clay degradation, fallback records."""
from __future__ import annotations

from unittest.mock import patch

from agents.sales_reps.pre_demo import linkedin_enrichment as le
from agents.sales_reps.integrations import clay


def test_empty_inputs_return_empty():
    with patch.object(clay, "enrich_contact") as e, \
         patch.object(clay, "find_decision_makers") as d:
        out = le.enrich_for_brief(None, [])
    assert out == []
    e.assert_not_called()
    d.assert_not_called()


def test_attendees_are_enriched_and_marked_attending():
    def fake_enrich(email):
        return {"name": "A", "email": email, "title": "CFO", "linkedin_url": "x", "source": "clay"}
    with patch.object(clay, "enrich_contact", side_effect=fake_enrich), \
         patch.object(clay, "find_decision_makers", return_value=[]):
        out = le.enrich_for_brief("acme.com", ["buyer@acme.com"])
    assert len(out) == 1
    assert out[0]["attending"] is True
    assert out[0]["email"] == "buyer@acme.com"


def test_attendee_falls_back_to_minimal_record_on_clay_miss():
    with patch.object(clay, "enrich_contact", return_value=None), \
         patch.object(clay, "find_decision_makers", return_value=[]):
        out = le.enrich_for_brief("acme.com", ["ghost@acme.com"])
    assert len(out) == 1
    assert out[0]["email"] == "ghost@acme.com"
    assert out[0]["source"] == "attendee_only"


def test_committee_dedupes_against_attendees():
    attendee = {"name": "Alex", "email": "alex@acme.com", "title": "CFO",
                "linkedin_url": "x", "source": "clay"}
    committee = [
        {"name": "Alex", "email": "alex@acme.com", "title": "CFO"},     # dup
        {"name": "Beth", "email": "beth@acme.com", "title": "VP Ops"},   # new
    ]
    with patch.object(clay, "enrich_contact", return_value=attendee), \
         patch.object(clay, "find_decision_makers",
                      return_value=[clay._normalize_person(c) for c in committee]):
        out = le.enrich_for_brief("acme.com", ["alex@acme.com"])
    emails = [p["email"] for p in out]
    assert emails == ["alex@acme.com", "beth@acme.com"]


def test_committee_capped_at_five():
    attendee = {"name": "Alex", "email": "alex@acme.com", "title": "CFO",
                "linkedin_url": None, "source": "clay"}
    committee = [
        {"name": f"P{i}", "email": f"p{i}@acme.com", "title": "VP"} for i in range(10)
    ]
    with patch.object(clay, "enrich_contact", return_value=attendee), \
         patch.object(clay, "find_decision_makers",
                      return_value=[clay._normalize_person(c) for c in committee]):
        out = le.enrich_for_brief("acme.com", ["alex@acme.com"])
    non_attending = [p for p in out if not p.get("attending")]
    assert len(non_attending) == 5


def test_committee_skipped_when_domain_missing():
    with patch.object(clay, "enrich_contact", return_value=None), \
         patch.object(clay, "find_decision_makers") as d:
        out = le.enrich_for_brief(None, ["a@b.com"])
    d.assert_not_called()
    assert len(out) == 1


def test_committee_degrades_on_clay_error():
    with patch.object(clay, "enrich_contact", return_value=None), \
         patch.object(clay, "find_decision_makers",
                      side_effect=clay.ClayError("rate limited")):
        out = le.enrich_for_brief("acme.com", ["a@acme.com"])
    # Attendee fallback still present; committee is empty.
    assert len(out) == 1
