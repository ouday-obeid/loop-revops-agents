"""Pre-demo brief generator — resolve, compose, degrade, render."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from agents.sales_reps.pre_demo import brief_generator as bg


# --------------------------------------------------------------- resolver

def test_resolve_opportunity_by_id():
    fake = {"records": [{"Id": "006ABCDEFGHIJKLMN", "Name": "Acme",
                         "StageName": "Proposal", "Amount": 25000,
                         "Account": {"Name": "Acme", "Website": "acme.com"},
                         "Owner": {"Email": "ae@tryloop.ai"}}]}
    with patch.object(bg.salesforce_mcp, "soql_query", return_value=fake):
        out = bg._resolve_opportunity("006ABCDEFGHIJKLMN")
    assert out["Id"] == "006ABCDEFGHIJKLMN"


def test_resolve_opportunity_by_account_name():
    fake = {"records": [{"Id": "006XYZ", "Name": "Acme Loop", "StageName": "Demo",
                         "Account": {"Name": "Acme Corp", "Website": "acme.com"},
                         "Owner": {"Email": "ae@tryloop.ai"}}]}
    with patch.object(bg.salesforce_mcp, "soql_query", return_value=fake):
        out = bg._resolve_opportunity("Acme Corp")
    assert out["Id"] == "006XYZ"


def test_resolve_opportunity_none_on_empty():
    with patch.object(bg.salesforce_mcp, "soql_query", return_value={"records": []}):
        assert bg._resolve_opportunity("Unknown Co") is None


def test_resolve_opportunity_degrades_on_soql_error():
    with patch.object(bg.salesforce_mcp, "soql_query", side_effect=RuntimeError("boom")):
        assert bg._resolve_opportunity("Anything") is None


def test_resolve_opportunity_escapes_single_quotes():
    # Verify the SOQL we send escapes injection attempts; no exception expected.
    with patch.object(bg.salesforce_mcp, "soql_query", return_value={"records": []}) as m:
        bg._resolve_opportunity("O'Malley Pizza")
    sent_query = m.call_args.args[0]
    assert "O\\'Malley" in sent_query


# --------------------------------------------------------------- helpers

def test_domain_from_website_strips_www():
    assert bg._domain_from_website("https://www.acme.com") == "acme.com"
    assert bg._domain_from_website("acme.com") == "acme.com"
    assert bg._domain_from_website("  http://ACME.com/path  ") == "acme.com"


def test_domain_from_website_returns_none_for_blank():
    assert bg._domain_from_website(None) is None
    assert bg._domain_from_website("") is None


def test_list_account_contact_emails_excludes_internal():
    fake = {"records": [
        {"Email": "buyer@acme.com"},
        {"Email": "ops@acme.com"},
        {"Email": "partner@tryloop.ai"},
    ]}
    with patch.object(bg.salesforce_mcp, "soql_query", return_value=fake):
        out = bg._list_account_contact_emails("001ABC")
    assert out == ["buyer@acme.com", "ops@acme.com"]


def test_list_account_contact_emails_empty_when_no_account():
    assert bg._list_account_contact_emails(None) == []


# --------------------------------------------------------------- Fireflies

def test_prior_calls_dedupes_by_id():
    with patch.object(bg.fireflies_mcp, "list_transcripts", return_value=[
        {"id": "MTG_1", "title": "A", "date": "2026-04-10"},
        {"id": "MTG_1", "title": "A", "date": "2026-04-10"},  # dup
        {"id": "MTG_2", "title": "B", "date": "2026-04-11"},
    ]):
        out = bg._prior_calls(["buyer@acme.com"])
    assert len(out) == 2


def test_prior_calls_empty_without_emails():
    assert bg._prior_calls([]) == []


def test_prior_calls_degrades_on_fireflies_error():
    with patch.object(bg.fireflies_mcp, "list_transcripts",
                      side_effect=RuntimeError("fireflies 500")):
        out = bg._prior_calls(["buyer@acme.com"])
    assert out == []


# --------------------------------------------------------------- knowledge + news/funding

def test_knowledge_hits_empty_for_no_account_name():
    assert bg._knowledge_hits(None) == []
    assert bg._knowledge_hits("") == []


def test_knowledge_hits_trims_content_and_preserves_metadata():
    hits = [{"content": "x" * 1000, "score": 0.9, "metadata": {"source": "gong"}}]
    with patch.object(bg.knowledge_mcp, "semantic_search", return_value=hits):
        out = bg._knowledge_hits("Acme Corp")
    assert len(out[0]["snippet"]) == 500
    assert out[0]["metadata"]["source"] == "gong"


def test_knowledge_hits_degrades_on_search_error():
    with patch.object(bg.knowledge_mcp, "semantic_search",
                      side_effect=RuntimeError("chroma down")):
        out = bg._knowledge_hits("Acme Corp")
    assert out == []


def test_news_and_funding_empty_without_domain():
    news, funding = bg._news_and_funding(None)
    assert news == [] and funding == []


def test_news_and_funding_degrades_on_news_exception():
    # Simulate web_research.fetch_company_news raising a non-httpx RuntimeError
    # (the wrapper swallows httpx errors internally; this tests the outer guard).
    from agents.sales_reps.integrations import web_research
    with patch.object(web_research, "fetch_company_news",
                      side_effect=RuntimeError("unexpected")), \
         patch.object(web_research, "fetch_funding_events", return_value=[]):
        news, funding = bg._news_and_funding("acme.com")
    assert news == []
    assert funding == []


def test_news_and_funding_degrades_on_funding_exception():
    from agents.sales_reps.integrations import web_research
    with patch.object(web_research, "fetch_company_news", return_value=[]), \
         patch.object(web_research, "fetch_funding_events",
                      side_effect=RuntimeError("unexpected")):
        news, funding = bg._news_and_funding("acme.com")
    assert news == []
    assert funding == []


# --------------------------------------------------------------- talking points + gaps

def test_talking_points_discovery_stage_asks_about_timeline():
    opp = {"StageName": "Discovery"}
    pts = bg._derive_talking_points(opp, [], [])
    assert any("timeline" in p.lower() for p in pts)


def test_talking_points_proposal_follows_up_objections():
    opp = {"StageName": "Proposal"}
    pts = bg._derive_talking_points(opp, [], [])
    assert any("objection" in p.lower() for p in pts)


def test_talking_points_congratulates_on_funding():
    opp = {"StageName": "Demo"}
    funding = [{"amount_usd": 10_000_000, "type": "Series A", "announced_at": "2026-03-01"}]
    pts = bg._derive_talking_points(opp, [], funding)
    assert any("Congratulate" in p for p in pts)


def test_gaps_flags_missing_amount_and_close():
    opp = {"Amount": None, "CloseDate": None}
    gaps = bg._derive_gaps(opp, [])
    assert any("Amount" in g for g in gaps)
    assert any("CloseDate" in g for g in gaps)


def test_gaps_flags_single_threaded():
    opp = {"Amount": 10000, "CloseDate": "2026-05-01"}
    people = [{"name": "solo", "attending": True, "title": "CEO"}]
    gaps = bg._derive_gaps(opp, people)
    assert any("Single-threaded" in g for g in gaps)


# --------------------------------------------------------------- generate()

def test_generate_empty_target_returns_usage():
    out = asyncio.run(bg.generate(""))
    assert "Usage" in out["text"]


def test_generate_not_found():
    with patch.object(bg, "_resolve_opportunity", return_value=None):
        out = asyncio.run(bg.generate("Nonexistent Co"))
    assert out["error"] == "not_found"


def test_generate_happy_path_assembles_all_sources():
    opp = {
        "Id": "006ABC", "Name": "Acme Loop", "StageName": "Proposal",
        "Amount": 25000, "CloseDate": "2026-05-01", "AccountId": "001A",
        "Account": {"Name": "Acme", "Website": "https://acme.com"},
        "Owner": {"Email": "ae@tryloop.ai"},
    }
    with patch.object(bg, "_resolve_opportunity", return_value=opp), \
         patch.object(bg, "_list_account_contact_emails", return_value=["buyer@acme.com"]), \
         patch.object(bg.linkedin_enrichment, "enrich_for_brief",
                      return_value=[{"name": "Alex", "email": "buyer@acme.com",
                                     "title": "CFO", "attending": True,
                                     "linkedin_url": "https://linkedin.com/in/alex"}]), \
         patch.object(bg, "_prior_calls", return_value=[
             {"id": "MTG_1", "title": "Intro", "date": "2026-04-01"}]), \
         patch.object(bg, "_knowledge_hits", return_value=[
             {"snippet": "uses Toast today", "score": 0.8}]), \
         patch.object(bg, "_news_and_funding", return_value=(
             [{"title": "Acme raises", "url": "https://news.com", "source": "TC"}],
             [{"type": "Series B", "amount_usd": 25_000_000, "announced_at": "2026-03-01"}],
         )):
        out = asyncio.run(bg.generate("006ABC"))
    assert out["opportunity_id"] == "006ABC"
    assert out["domain"] == "acme.com"
    assert len(out["people"]) == 1
    assert out["prior_calls"][0]["id"] == "MTG_1"
    assert len(out["talking_points"]) >= 1
    assert "Acme" in out["text"]


def test_generate_assembles_without_domain_when_website_missing():
    opp = {
        "Id": "006NOSITE", "Name": "Stealth Co", "StageName": "Discovery",
        "Account": {"Name": "Stealth Co", "Website": None},
        "Owner": {"Email": "ae@tryloop.ai"},
    }
    with patch.object(bg, "_resolve_opportunity", return_value=opp), \
         patch.object(bg, "_list_account_contact_emails", return_value=[]), \
         patch.object(bg.linkedin_enrichment, "enrich_for_brief", return_value=[]), \
         patch.object(bg, "_prior_calls", return_value=[]), \
         patch.object(bg, "_knowledge_hits", return_value=[]), \
         patch.object(bg, "_news_and_funding", return_value=([], [])):
        out = asyncio.run(bg.generate("Stealth Co"))
    assert out["domain"] is None
    assert out["news"] == []
    # Still renders a text body.
    assert "Stealth Co" in out["text"]


def test_generate_include_blocks_flag():
    opp = {
        "Id": "006X", "Name": "X", "StageName": "Demo",
        "Account": {"Name": "X", "Website": "x.com"},
        "Owner": {"Email": "ae@tryloop.ai"},
    }
    with patch.object(bg, "_resolve_opportunity", return_value=opp), \
         patch.object(bg, "_list_account_contact_emails", return_value=[]), \
         patch.object(bg.linkedin_enrichment, "enrich_for_brief", return_value=[]), \
         patch.object(bg, "_prior_calls", return_value=[]), \
         patch.object(bg, "_knowledge_hits", return_value=[]), \
         patch.object(bg, "_news_and_funding", return_value=([], [])):
        out = asyncio.run(bg.generate("006X", include_blocks=True))
    assert "blocks" in out
    assert out["blocks"][0]["type"] == "header"
