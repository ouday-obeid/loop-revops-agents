"""Clay client — decision-maker lookup + single-contact enrichment."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.sales_reps.integrations import clay


def _mock_resp(status: int = 200, payload: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload or {}
    resp.text = "body"
    return resp


def _mock_client_returning(resp) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.post.return_value = resp
    return client


def test_normalize_person_flattens_fields():
    raw = {"full_name": "Alex Buyer", "email": "ALEX@acme.com", "job_title": "CFO",
           "linkedin": "https://linkedin.com/in/alex", "company": {"name": "Acme"}}
    out = clay._normalize_person(raw)
    assert out["name"] == "Alex Buyer"
    assert out["email"] == "alex@acme.com"
    assert out["title"] == "CFO"
    assert out["company"] == "Acme"
    assert out["linkedin_url"] == "https://linkedin.com/in/alex"


def test_normalize_person_handles_string_company():
    out = clay._normalize_person({"name": "A", "company": "Plain String Co"})
    assert out["company"] == "Plain String Co"


def test_find_decision_makers_empty_domain_returns_empty():
    # No HTTP call at all for an empty domain.
    assert clay.find_decision_makers("") == []


def test_find_decision_makers_normalizes_results():
    payload = {"people": [
        {"full_name": "A", "email": "a@acme.com", "job_title": "CEO"},
        {"full_name": "B", "email": "b@acme.com", "job_title": "CFO"},
    ]}
    resp = _mock_resp(200, payload)
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)):
        out = clay.find_decision_makers("acme.com", limit=5)
    assert len(out) == 2
    assert out[0]["email"] == "a@acme.com"


def test_find_decision_makers_handles_results_key_variant():
    payload = {"results": [{"name": "X", "email": "x@y.com"}]}
    resp = _mock_resp(200, payload)
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)):
        out = clay.find_decision_makers("y.com")
    assert len(out) == 1


def test_enrich_contact_returns_none_for_empty_email():
    assert clay.enrich_contact("") is None
    assert clay.enrich_contact(None) is None


def test_enrich_contact_returns_person_on_hit():
    payload = {"person": {"name": "Alex", "email": "alex@acme.com", "title": "VP Ops"}}
    resp = _mock_resp(200, payload)
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)):
        out = clay.enrich_contact("alex@acme.com")
    assert out is not None
    assert out["name"] == "Alex"


def test_enrich_contact_returns_none_when_not_found():
    resp = _mock_resp(200, {"person": {}})
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)):
        out = clay.enrich_contact("who@nowhere.com")
    assert out is None


def test_enrich_contact_swallows_clay_error():
    # A 500 from Clay mid-enrichment must not crash the brief.
    resp = _mock_resp(500, {"error": "boom"})
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)):
        out = clay.enrich_contact("a@b.com")
    assert out is None


def test_post_raises_on_http_error():
    resp = _mock_resp(400, {})
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)), \
         pytest.raises(clay.ClayError):
        clay._post("/people/search", {"q": "acme"})


def test_post_raises_on_non_json():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = "<html>err</html>"
    resp.json.side_effect = ValueError("not JSON")
    with patch.object(clay, "_client", return_value=_mock_client_returning(resp)), \
         pytest.raises(clay.ClayError):
        clay._post("/people/search", {"q": "acme"})


def test_base_url_uses_override(monkeypatch):
    monkeypatch.setenv("CLAY_BASE_URL", "https://eu.clay.com/api/")
    assert clay._base_url() == "https://eu.clay.com/api"
