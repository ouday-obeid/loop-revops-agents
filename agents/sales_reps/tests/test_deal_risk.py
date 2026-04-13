"""Deal-risk — detectors, severity classification, Slack rendering, degradation."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from agents.sales_reps import deal_risk as dr


# --------------------------------------------------------------- pushed_close

def test_detect_pushed_close_ignores_sub_threshold():
    # 5 day push — below the 15d threshold → no signal.
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "2026-05-01", "NewValue": "2026-05-06",
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": [{
        "Id": "006A", "Name": "Acme", "StageName": "Proposal",
        "Amount": 10000, "IsClosed": False, "Owner": {"Email": "ae@x.com"},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_pushed_close()
    assert out == []


def test_detect_pushed_close_flags_large_push():
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "2026-05-01", "NewValue": "2026-06-05",  # 35 days
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": [{
        "Id": "006A", "Name": "Acme", "StageName": "Negotiation",
        "Amount": 25000, "IsClosed": False, "Owner": {"Email": "ae@x.com"},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_pushed_close()
    assert len(out) == 1
    assert out[0].signal == "pushed_close"
    assert out[0].severity == "high"
    assert "+35d" in out[0].evidence


def test_detect_pushed_close_ignores_pulled_in_dates():
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "2026-05-01", "NewValue": "2026-04-01",  # pulled IN, not pushed
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": [{
        "Id": "006A", "Name": "Acme", "StageName": "Proposal",
        "Amount": 10000, "IsClosed": False, "Owner": {"Email": "ae@x.com"},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_pushed_close()
    assert out == []


def test_detect_pushed_close_skips_closed_opps():
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "2026-05-01", "NewValue": "2026-07-01",
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": []}  # no open opp returned
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_pushed_close()
    assert out == []


# --------------------------------------------------------------- amount_drop

def test_detect_amount_drops_flags_20_percent():
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "10000", "NewValue": "7500",  # 25% drop
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": [{
        "Id": "006A", "Name": "Acme", "StageName": "Negotiation",
        "Amount": 7500, "IsClosed": False, "Owner": {"Email": "ae@x.com"},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_amount_drops()
    assert len(out) == 1
    assert out[0].signal == "amount_drop"
    assert out[0].severity == "warn"


def test_detect_amount_drops_high_severity_over_50pct():
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "10000", "NewValue": "4000",  # 60% drop
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": [{
        "Id": "006A", "Name": "Acme", "StageName": "Negotiation",
        "Amount": 4000, "IsClosed": False, "Owner": {"Email": "ae@x.com"},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_amount_drops()
    assert out[0].severity == "high"


def test_detect_amount_drops_ignores_increases():
    history = {"records": [{
        "OpportunityId": "006A",
        "OldValue": "10000", "NewValue": "15000",  # increase, not drop
        "CreatedDate": "2026-04-10T00:00:00Z",
    }]}
    opps = {"records": [{
        "Id": "006A", "Name": "Acme", "StageName": "Proposal",
        "Amount": 15000, "IsClosed": False, "Owner": {"Email": "ae@x.com"},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", side_effect=[history, opps]):
        out = dr._detect_amount_drops()
    assert out == []


# --------------------------------------------------------------- champion_gone

def test_detect_champion_gone_flags_deactivated_primary():
    rows = {"records": [{
        "Id": "OCR_1",
        "OpportunityId": "006A",
        "Role": "Champion",
        "IsPrimary": True,
        "Contact": {
            "Id": "003A", "Name": "Chris Champ",
            "Active__c": False, "LastModifiedDate": "2026-04-10T00:00:00Z",
        },
        "Opportunity": {
            "Name": "Acme", "StageName": "Negotiation",
            "Amount": 20000, "IsClosed": False,
            "Owner": {"Email": "ae@x.com"},
        },
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", return_value=rows):
        out = dr._detect_champion_gone()
    assert len(out) == 1
    assert out[0].signal == "champion_gone"
    assert out[0].severity == "high"
    assert "Chris Champ" in out[0].evidence


def test_detect_champion_gone_skips_closed_opps():
    rows = {"records": [{
        "Id": "OCR_1", "OpportunityId": "006A", "IsPrimary": True,
        "Contact": {"Id": "003", "Name": "X", "Active__c": False,
                    "LastModifiedDate": "2026-04-01T00:00:00Z"},
        "Opportunity": {"Name": "Acme", "StageName": "Closed Won",
                        "Amount": 1000, "IsClosed": True,
                        "Owner": {"Email": "ae@x.com"}},
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", return_value=rows):
        out = dr._detect_champion_gone()
    assert out == []


def test_detect_champion_gone_degrades_on_missing_field():
    # Active__c may not exist in all orgs — detector must degrade, not crash.
    with patch.object(dr.salesforce_mcp, "soql_query",
                      side_effect=RuntimeError("INVALID_FIELD Active__c")):
        out = dr._detect_champion_gone()
    assert out == []


# --------------------------------------------------------------- competitor_mention

def test_opp_for_attendees_returns_first_match():
    fake = {"records": [{
        "OpportunityId": "006Z",
        "Opportunity": {
            "Name": "Best", "StageName": "Demo",
            "Amount": 5000, "IsClosed": False,
            "Owner": {"Email": "ae@x.com"},
        },
    }]}
    with patch.object(dr.salesforce_mcp, "soql_query", return_value=fake):
        opp = dr._opp_for_attendees(["buyer@external.com"])
    assert opp["Id"] == "006Z"


def test_opp_for_attendees_empty_returns_none():
    assert dr._opp_for_attendees([]) is None


def test_detect_competitor_mentions_flags_hit():
    transcripts_list = [{"id": "MTG_C"}]
    transcript = {
        "id": "MTG_C",
        "participants": "ae@tryloop.ai, buyer@external.com",
        "sentences": [
            {"text": "we are evaluating MarginEdge too"},
            {"text": "ok"},
        ],
    }
    opp_fake = {"records": [{
        "OpportunityId": "006C",
        "Opportunity": {
            "Name": "CompTest", "StageName": "Proposal",
            "Amount": 7500, "IsClosed": False,
            "Owner": {"Email": "ae@x.com"},
        },
    }]}
    with patch.object(dr.fireflies_mcp, "list_transcripts", return_value=transcripts_list), \
         patch.object(dr.fireflies_mcp, "get_transcript", return_value=transcript), \
         patch.object(dr.salesforce_mcp, "soql_query", return_value=opp_fake):
        out = dr._detect_competitor_mentions()
    assert len(out) == 1
    assert out[0].signal == "competitor_mention"
    assert "MarginEdge" in out[0].evidence


def test_detect_competitor_mentions_no_hit_returns_empty():
    transcripts_list = [{"id": "MTG_N"}]
    transcript = {
        "id": "MTG_N",
        "participants": ["ae@tryloop.ai", "buyer@external.com"],
        "sentences": [{"text": "we use excel and sheets"}],
    }
    with patch.object(dr.fireflies_mcp, "list_transcripts", return_value=transcripts_list), \
         patch.object(dr.fireflies_mcp, "get_transcript", return_value=transcript):
        out = dr._detect_competitor_mentions()
    assert out == []


def test_detect_competitor_mentions_degrades_on_fireflies_error():
    with patch.object(dr.fireflies_mcp, "list_transcripts",
                      side_effect=RuntimeError("fireflies down")):
        out = dr._detect_competitor_mentions()
    assert out == []


def test_competitor_pattern_matches_known_names():
    p = dr._competitor_pattern()
    assert p.search("we are evaluating MarginEdge right now")
    assert p.search("what about Restaurant365?")
    assert p.search("they use r365 today") is not None  # case-insensitive
    assert p.search("we use excel") is None


# --------------------------------------------------------------- run_sweep

def test_run_sweep_aggregates_all_detectors():
    with patch.object(dr, "_detect_pushed_close", return_value=[
        dr.RiskSignal("006A", "Acme", "ae@x.com", "Proposal", 10000,
                      "pushed_close", "warn", "+20d"),
    ]), patch.object(dr, "_detect_amount_drops", return_value=[]), \
         patch.object(dr, "_detect_champion_gone", return_value=[]), \
         patch.object(dr, "_detect_competitor_mentions", return_value=[]):
        out = asyncio.run(dr.run_sweep())
    assert out["total_signals"] == 1
    assert out["errors"] == []
    assert "Deal risk sweep" in out["text"]
    assert "WARN" in out["text"]


def test_run_sweep_isolates_failing_detector():
    with patch.object(dr, "_detect_pushed_close", side_effect=RuntimeError("boom")), \
         patch.object(dr, "_detect_amount_drops", return_value=[]), \
         patch.object(dr, "_detect_champion_gone", return_value=[]), \
         patch.object(dr, "_detect_competitor_mentions", return_value=[]):
        out = asyncio.run(dr.run_sweep())
    assert "pushed_close:RuntimeError" in out["errors"]
    # Sweep keeps going with other detectors.
    assert out["total_signals"] == 0


def test_run_sweep_empty_reports_no_signals():
    with patch.object(dr, "_detect_pushed_close", return_value=[]), \
         patch.object(dr, "_detect_amount_drops", return_value=[]), \
         patch.object(dr, "_detect_champion_gone", return_value=[]), \
         patch.object(dr, "_detect_competitor_mentions", return_value=[]):
        out = asyncio.run(dr.run_sweep())
    assert "no new signals" in out["text"].lower()


# --------------------------------------------------------------- rendering

def test_render_slack_groups_by_severity():
    signals = [
        dr.RiskSignal("006A", "A", "ae@x.com", "Proposal", 1, "pushed_close", "high", "e"),
        dr.RiskSignal("006B", "B", "ae@x.com", "Proposal", 1, "amount_drop", "warn", "e"),
    ]
    out = dr._render_slack(signals)
    assert "HIGH" in out
    assert "WARN" in out
    assert "006A" in out
    assert "006B" in out
