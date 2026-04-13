"""Momentum API client — normalization, HTTP error handling, auth wiring."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from agents.sales_reps.integrations import momentum


def _mock_response(status: int = 200, payload: dict | None = None) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = payload or {}
    resp.text = "body"
    return resp


def test_normalize_call_flattens_nested_rep_and_contact():
    raw = {
        "id": "CALL_1",
        "start_time": "2026-04-13T15:00:00Z",
        "duration": 420,
        "direction": "OUTBOUND",
        "rep": {"email": "Rep@tryloop.ai", "name": "Rep One"},
        "contact": {"email": "buyer@acme.com", "phone": "+15550000"},
        "salesforce": {"task_id": "00T001", "synced": True},
    }
    out = momentum._normalize_call(raw)
    assert out["id"] == "CALL_1"
    assert out["rep_email"] == "rep@tryloop.ai"  # lowercased
    assert out["contact_email"] == "buyer@acme.com"
    assert out["direction"] == "outbound"
    assert out["duration_seconds"] == 420
    assert out["sf_task_id"] == "00T001"
    assert out["sf_synced"] is True


def test_normalize_call_handles_flat_legacy_shape():
    raw = {
        "id": "CALL_2",
        "timestamp": "2026-04-13T15:00:00Z",
        "duration_seconds": 90,
        "rep_email": "X@Y.com",
        "contact_email": "Z@Q.com",
        "sf_synced": False,
    }
    out = momentum._normalize_call(raw)
    assert out["rep_email"] == "x@y.com"
    assert out["contact_email"] == "z@q.com"
    assert out["sf_synced"] is False


def test_normalize_call_defaults_empty_strings_to_none():
    out = momentum._normalize_call({"id": "CALL_3"})
    assert out["rep_email"] is None
    assert out["contact_email"] is None
    assert out["duration_seconds"] == 0


def test_list_recent_calls_normalizes_response():
    payload = {"calls": [
        {"id": "CALL_A", "rep": {"email": "a@tryloop.ai"}, "duration_seconds": 60,
         "start_time": "2026-04-13T12:00:00Z"},
        {"id": "CALL_B", "rep": {"email": "b@tryloop.ai"}, "duration_seconds": 120,
         "start_time": "2026-04-13T13:00:00Z"},
    ]}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.get.return_value = _mock_response(200, payload)
    with patch.object(momentum, "_client", return_value=client), \
         patch("agents.sales_reps.integrations.momentum.require_secret", return_value="tok"):
        out = momentum.list_recent_calls(hours=2, limit=100)
    assert len(out) == 2
    assert out[0]["rep_email"] == "a@tryloop.ai"


def test_list_recent_calls_filters_rows_without_id():
    payload = {"calls": [
        {"id": "CALL_A", "rep": {"email": "a@tryloop.ai"}},
        {"rep": {"email": "b@tryloop.ai"}},  # no id — dropped
    ]}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.get.return_value = _mock_response(200, payload)
    with patch.object(momentum, "_client", return_value=client):
        out = momentum.list_recent_calls()
    assert len(out) == 1
    assert out[0]["id"] == "CALL_A"


def test_list_recent_calls_handles_data_key_variant():
    # Some Momentum versions return {"data": [...]} instead of {"calls": [...]}.
    payload = {"data": [{"id": "CALL_X"}]}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.get.return_value = _mock_response(200, payload)
    with patch.object(momentum, "_client", return_value=client):
        out = momentum.list_recent_calls()
    assert len(out) == 1
    assert out[0]["id"] == "CALL_X"


def test_get_raises_on_non_200():
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.get.return_value = _mock_response(500, {})
    with patch.object(momentum, "_client", return_value=client), \
         pytest.raises(momentum.MomentumError):
        momentum._get("/v1/calls")


def test_get_raises_on_non_json():
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = 200
    resp.text = "<html>gateway</html>"
    resp.json.side_effect = ValueError("not JSON")
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.get.return_value = resp
    with patch.object(momentum, "_client", return_value=client), \
         pytest.raises(momentum.MomentumError):
        momentum._get("/v1/calls")


def test_base_url_uses_config_override(monkeypatch):
    monkeypatch.setenv("MOMENTUM_BASE_URL", "https://custom.momentum.io/")
    assert momentum._base_url() == "https://custom.momentum.io"


def test_base_url_defaults_when_missing(monkeypatch):
    monkeypatch.delenv("MOMENTUM_BASE_URL", raising=False)
    assert momentum._base_url() == "https://api.momentum.io"


def test_get_call_normalizes_single_record():
    payload = {"call": {"id": "CALL_Q", "rep": {"email": "r@tryloop.ai"},
                        "duration_seconds": 30}}
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = None
    client.get.return_value = _mock_response(200, payload)
    with patch.object(momentum, "_client", return_value=client):
        out = momentum.get_call("CALL_Q")
    assert out["id"] == "CALL_Q"
    assert out["rep_email"] == "r@tryloop.ai"
