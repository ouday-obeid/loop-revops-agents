"""Cover fireflies_mcp (mocked httpx) + secrets module paths."""
import os
from unittest.mock import MagicMock, patch


def test_secrets_require_missing_raises(monkeypatch):
    from shared import secrets
    monkeypatch.delenv("DEFINITELY_NOT_SET_VAR", raising=False)
    val = secrets.get_secret("DEFINITELY_NOT_SET_VAR")
    assert val is None
    try:
        secrets.require_secret("DEFINITELY_NOT_SET_VAR")
    except RuntimeError:
        pass
    else:
        raise AssertionError("require_secret should have raised")


def test_secrets_unknown_backend(monkeypatch):
    from shared import secrets
    monkeypatch.setenv("REVOPS_SECRETS_BACKEND", "bogus")
    try:
        secrets.get_secret("ANY")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")
    monkeypatch.setenv("REVOPS_SECRETS_BACKEND", "dotenv")


def test_fireflies_list_transcripts_mocked(monkeypatch):
    monkeypatch.setenv("FIREFLIES_API_KEY", "fake-key")
    from shared.mcp import fireflies_mcp

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"data": {"transcripts": [{"id": "x", "title": "t"}]}}
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.post.return_value = fake_response

    with patch.object(fireflies_mcp, "_client", return_value=fake_client):
        rows = fireflies_mcp.list_transcripts(limit=1)
    assert rows == [{"id": "x", "title": "t"}]


def test_fireflies_http_error(monkeypatch):
    monkeypatch.setenv("FIREFLIES_API_KEY", "fake-key")
    from shared.mcp import fireflies_mcp

    fake_response = MagicMock()
    fake_response.status_code = 500
    fake_response.text = "boom"
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False
    fake_client.post.return_value = fake_response

    with patch.object(fireflies_mcp, "_client", return_value=fake_client):
        try:
            fireflies_mcp.list_transcripts(limit=1)
        except fireflies_mcp.FirefliesError:
            pass
        else:
            raise AssertionError("expected FirefliesError")


def test_salesforce_soql_mocked():
    from shared.mcp import salesforce_mcp
    with patch.object(salesforce_mcp, "_sf", return_value={"records": [{"Id": "1"}], "totalSize": 1}):
        r = salesforce_mcp.soql_query("SELECT Id FROM Account")
    assert r["totalSize"] == 1


def test_salesforce_describe_sobject_mocked():
    from shared.mcp import salesforce_mcp
    with patch.object(salesforce_mcp, "_sf", return_value={"name": "Account", "fields": []}):
        r = salesforce_mcp.describe_sobject("Account")
    assert r["name"] == "Account"


def test_salesforce_list_users_mocked():
    from shared.mcp import salesforce_mcp
    with patch.object(salesforce_mcp, "_sf", return_value={"records": [{"Id": "1"}, {"Id": "2"}]}):
        users = salesforce_mcp.list_users()
    assert len(users) == 2
