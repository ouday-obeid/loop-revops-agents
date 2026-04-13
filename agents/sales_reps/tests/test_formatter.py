"""Pre-demo Slack formatter — minimal happy path + field guards."""
from __future__ import annotations

from agents.sales_reps.pre_demo import formatter


def _brief(**overrides):
    base = {
        "account_name": "Acme Corp",
        "opportunity_name": "Acme - Loop",
        "stage": "Proposal",
        "amount": 25000.0,
        "close_date": "2026-05-01",
        "people": [
            {"name": "Alex Buyer", "email": "a@acme.com", "title": "CFO",
             "linkedin_url": "https://linkedin.com/in/alex", "attending": True},
            {"name": "Beth Ops", "email": "b@acme.com", "title": "VP Ops",
             "attending": False},
        ],
        "prior_calls": [{"title": "Intro", "date": "2026-04-01T12:00:00Z"}],
        "knowledge": [{"snippet": "Uses Toast today"}],
        "news": [{"title": "Acme raises", "url": "https://news.com", "source": "TC"}],
        "funding": [{"type": "Series B", "amount_usd": 25_000_000,
                     "announced_at": "2026-03-01"}],
        "talking_points": ["Confirm decision timeline"],
        "gaps": ["CloseDate not set"],
    }
    base.update(overrides)
    return base


def test_to_slack_text_contains_header_and_sections():
    out = formatter.to_slack_text(_brief())
    assert "Acme Corp" in out
    assert "Proposal" in out
    assert "CFO" in out
    assert "Intro" in out
    assert "Acme raises" in out
    assert "Confirm decision timeline" in out


def test_to_slack_text_handles_missing_amount():
    out = formatter.to_slack_text(_brief(amount=None))
    assert "Amount: —" in out


def test_to_slack_text_handles_empty_people():
    out = formatter.to_slack_text(_brief(people=[]))
    # Section header should not appear when the list is empty.
    assert "Who's on the call" not in out


def test_to_slack_text_handles_no_news_no_funding():
    out = formatter.to_slack_text(_brief(news=[], funding=[]))
    assert "Recent news" not in out
    assert "Funding" not in out


def test_to_slack_text_truncates_knowledge_snippet():
    long = "x" * 500
    out = formatter.to_slack_text(_brief(knowledge=[{"snippet": long}]))
    # Only 180 chars + ellipsis rendered.
    assert "xxx…" in out


def test_to_slack_blocks_returns_header_plus_body():
    blocks = formatter.to_slack_blocks(_brief())
    assert len(blocks) == 2
    assert blocks[0]["type"] == "header"
    assert "Acme Corp" in blocks[0]["text"]["text"]
    assert blocks[1]["type"] == "section"


def test_to_slack_text_fallback_when_no_account_name():
    out = formatter.to_slack_text(_brief(account_name=None, opportunity_name="My Opp"))
    assert "My Opp" in out
