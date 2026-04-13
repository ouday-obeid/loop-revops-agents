"""Apollo news + funding client — key-missing degradation, HTTP errors, normalization."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from agents.sales_reps.integrations import web_research


def _mock_resp(status: int = 200, payload: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload or {}
    resp.text = "body"
    return resp


def _mock_client(resp, *, method: str = "post") -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    getattr(client, method).return_value = resp
    return client


def test_fetch_company_news_returns_empty_for_empty_domain():
    assert web_research.fetch_company_news("") == []


def test_fetch_company_news_returns_empty_when_key_missing(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    assert web_research.fetch_company_news("acme.com") == []


def test_fetch_company_news_normalizes_articles(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "tok")
    payload = {"news_articles": [
        {"title": "Acme raises", "url": "https://news.com/1",
         "publication_timestamp": "2026-04-01", "publisher": "TechCrunch"},
        {"title": "Acme launches", "url": "https://news.com/2",
         "publication_timestamp": "2026-04-02", "publisher": "Fortune"},
    ]}
    resp = _mock_resp(200, payload)
    with patch.object(web_research, "_apollo_client", return_value=_mock_client(resp)):
        out = web_research.fetch_company_news("acme.com")
    assert len(out) == 2
    assert out[0]["source"] == "TechCrunch"
    assert out[1]["url"] == "https://news.com/2"


def test_fetch_company_news_degrades_on_http_error(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "tok")
    resp = _mock_resp(500, {})
    with patch.object(web_research, "_apollo_client", return_value=_mock_client(resp)):
        out = web_research.fetch_company_news("acme.com")
    assert out == []


def test_fetch_company_news_degrades_on_network_error(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "tok")
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.post.side_effect = httpx.HTTPError("conn reset")
    with patch.object(web_research, "_apollo_client", return_value=client):
        out = web_research.fetch_company_news("acme.com")
    assert out == []


def test_fetch_funding_events_returns_empty_for_empty_domain():
    assert web_research.fetch_funding_events("") == []


def test_fetch_funding_events_normalizes_rounds(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "tok")
    payload = {"organization": {"funding_rounds": [
        {"round_type": "Series B", "amount_usd": 25_000_000,
         "announced_on": "2026-02-01", "investors": ["A", "B"]},
    ]}}
    resp = _mock_resp(200, payload)
    with patch.object(web_research, "_apollo_client",
                      return_value=_mock_client(resp, method="get")):
        out = web_research.fetch_funding_events("acme.com")
    assert len(out) == 1
    assert out[0]["amount_usd"] == 25_000_000
    assert out[0]["type"] == "Series B"


def test_fetch_funding_events_empty_when_no_rounds(monkeypatch):
    monkeypatch.setenv("APOLLO_API_KEY", "tok")
    resp = _mock_resp(200, {"organization": {}})
    with patch.object(web_research, "_apollo_client",
                      return_value=_mock_client(resp, method="get")):
        out = web_research.fetch_funding_events("acme.com")
    assert out == []
