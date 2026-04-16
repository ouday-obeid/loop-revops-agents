"""Coverage-raising tests for shared.mcp.salesforce_mcp.

Existing tests/test_salesforce_mcp_governance.py already covers
bulk_update gate enforcement + intent routing. This file fills the
read-path / write-path / error-path gaps (50% → >80% coverage gate
from Monday parent 11736844862).

All tests mock either `_sf` (function-level) or `subprocess.run`
(direct `_sf` testing) — never invoke the real `sf` CLI.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from shared.mcp import salesforce_mcp
from shared.mcp.salesforce_mcp import (
    SalesforceError,
    _needs_limit,
    _resolve_org_alias,
)


# ---------------------------------------------------- _resolve_org_alias

def test_resolve_org_alias_unknown_intent_raises():
    with pytest.raises(ValueError):
        _resolve_org_alias("not-a-real-intent")  # type: ignore[arg-type]


def test_resolve_org_alias_sandbox_requires_explicit_alias(monkeypatch):
    monkeypatch.delenv("SF_SANDBOX_ORG_ALIAS", raising=False)
    with pytest.raises(SalesforceError, match="SF_SANDBOX_ORG_ALIAS"):
        _resolve_org_alias("sandbox")


# ---------------------------------------------------- _needs_limit

def test_needs_limit_skips_when_already_limited():
    assert _needs_limit("SELECT Id FROM Account LIMIT 10") is False


def test_needs_limit_skips_aggregate_without_group_by():
    assert _needs_limit("SELECT COUNT(Id) FROM Account") is False
    assert _needs_limit("SELECT SUM(Amount) FROM Opportunity") is False


def test_needs_limit_keeps_when_aggregate_has_group_by():
    assert _needs_limit("SELECT COUNT(Id), StageName FROM Opportunity GROUP BY StageName") is True


def test_needs_limit_keeps_for_normal_select():
    assert _needs_limit("SELECT Id, Name FROM Account") is True


# ---------------------------------------------------- _sf subprocess wrapper

def _proc(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def test_sf_parses_json_payload_and_returns_result():
    payload = {"status": 0, "result": {"records": [{"Id": "001x"}], "totalSize": 1}}
    with patch("subprocess.run", return_value=_proc(stdout=json.dumps(payload))):
        out = salesforce_mcp._sf("data", "query", "--query", "SELECT Id FROM Account")
    assert out["records"][0]["Id"] == "001x"


def test_sf_returns_full_payload_when_result_missing():
    payload = {"status": 0, "totalSize": 0}
    with patch("subprocess.run", return_value=_proc(stdout=json.dumps(payload))):
        out = salesforce_mcp._sf("data", "query", "--query", "SELECT Id FROM Account")
    assert out["totalSize"] == 0


def test_sf_raises_on_non_json_stdout():
    with patch("subprocess.run", return_value=_proc(stdout="not json", returncode=1, stderr="boom")):
        with pytest.raises(SalesforceError, match="sf failed"):
            salesforce_mcp._sf("anything")


def test_sf_trusts_inner_success_when_outer_status_is_nonzero():
    """sf CLI sometimes returns outer status=1 with a cosmetic locale error
    but inner result.success=true. The wrapper must trust the inner state."""
    payload = {
        "status": 1,
        "message": "cosmetic locale error",
        "result": {"success": True, "id": "001ZZ"},
    }
    with patch("subprocess.run", return_value=_proc(stdout=json.dumps(payload))):
        out = salesforce_mcp._sf("data", "create", "record")
    assert out["success"] is True


def test_sf_raises_when_outer_nonzero_and_no_inner_success():
    payload = {"status": 1, "message": "real failure", "result": {}}
    with patch("subprocess.run", return_value=_proc(stdout=json.dumps(payload))):
        with pytest.raises(SalesforceError, match="real failure"):
            salesforce_mcp._sf("anything")


def test_sf_text_mode_returns_stdout():
    with patch("subprocess.run", return_value=_proc(stdout="hello text")):
        out = salesforce_mcp._sf("info", json_out=False)
    assert out["stdout"] == "hello text"


def test_sf_text_mode_raises_on_nonzero():
    with patch("subprocess.run", return_value=_proc(stdout="", stderr="oops", returncode=1)):
        with pytest.raises(SalesforceError, match="oops"):
            salesforce_mcp._sf("info", json_out=False)


# ---------------------------------------------------- Read wrappers

def test_soql_query_appends_limit_when_needed():
    with patch.object(salesforce_mcp, "_sf", return_value={"records": []}) as mock_sf:
        salesforce_mcp.soql_query("SELECT Id FROM Account", limit=25)
    args = mock_sf.call_args.args
    # Last positional arg is the constructed query
    query_idx = args.index("--query") + 1
    assert "LIMIT 25" in args[query_idx]


def test_soql_query_skips_limit_when_aggregate_no_group():
    with patch.object(salesforce_mcp, "_sf", return_value={"records": []}) as mock_sf:
        salesforce_mcp.soql_query("SELECT COUNT(Id) FROM Account", limit=10)
    query_idx = mock_sf.call_args.args.index("--query") + 1
    assert "LIMIT" not in mock_sf.call_args.args[query_idx]


def test_describe_sobject_passes_sobject_arg():
    with patch.object(salesforce_mcp, "_sf", return_value={"fields": []}) as mock_sf:
        salesforce_mcp.describe_sobject("Account")
    assert "--sobject" in mock_sf.call_args.args
    assert "Account" in mock_sf.call_args.args


def test_get_record_passes_sobject_and_id():
    with patch.object(salesforce_mcp, "_sf", return_value={"Id": "001x", "Name": "Acme"}) as mock_sf:
        salesforce_mcp.get_record("Account", "001x")
    args = mock_sf.call_args.args
    assert "--sobject" in args and "Account" in args
    assert "--record-id" in args and "001x" in args


def test_list_users_filters_active_when_flag_true():
    captured = {}
    def _fake_query(query, limit=100, *, intent="read"):
        captured["q"] = query
        return {"records": [{"Id": "005a", "Name": "U"}]}
    with patch.object(salesforce_mcp, "soql_query", side_effect=_fake_query):
        users = salesforce_mcp.list_users(active_only=True)
    assert "WHERE IsActive = true" in captured["q"]
    assert users[0]["Id"] == "005a"


def test_list_users_skips_filter_when_active_only_false():
    captured = {}
    def _fake_query(query, limit=100, *, intent="read"):
        captured["q"] = query
        return {"records": []}
    with patch.object(salesforce_mcp, "soql_query", side_effect=_fake_query):
        salesforce_mcp.list_users(active_only=False)
    assert "WHERE IsActive" not in captured["q"]


def test_describe_flow_uses_tooling_api():
    with patch.object(salesforce_mcp, "_sf", return_value={"records": []}) as mock_sf:
        salesforce_mcp.describe_flow("301xxx")
    assert "--use-tooling-api" in mock_sf.call_args.args


def test_tooling_query_appends_limit_when_specified():
    with patch.object(salesforce_mcp, "_sf", return_value={"records": []}) as mock_sf:
        salesforce_mcp.tooling_query("SELECT Id FROM ApexClass", limit=5)
    query_idx = mock_sf.call_args.args.index("--query") + 1
    assert "LIMIT 5" in mock_sf.call_args.args[query_idx]
    assert "--use-tooling-api" in mock_sf.call_args.args


def test_tooling_query_no_limit_when_none():
    with patch.object(salesforce_mcp, "_sf", return_value={"records": []}) as mock_sf:
        salesforce_mcp.tooling_query("SELECT Id FROM ApexClass", limit=None)
    query_idx = mock_sf.call_args.args.index("--query") + 1
    assert "LIMIT" not in mock_sf.call_args.args[query_idx]


# ---------------------------------------------------- Single-record writes

def test_create_record_requires_approved_gate():
    from shared.governance import ApprovalRequired
    with pytest.raises(ApprovalRequired):
        salesforce_mcp.create_record(
            "Account", {"Name": "Acme"}, agent_name="t", approval_gate_id=None
        )


def test_create_record_writes_audit_after_success():
    from shared import governance
    from sqlalchemy import text as sql_text
    from shared.db.connection import get_engine

    gid = governance.create_approval_gate(
        agent_name="t", action_type="single_record_update", payload={}, justification=None
    )
    governance.decide_approval_gate(gid, approved=True, approver="UT")
    with patch.object(salesforce_mcp, "_sf", return_value={"id": "001NEW", "success": True}):
        result = salesforce_mcp.create_record(
            "Account", {"Name": "Acme", "Phone": "555"},
            agent_name="t_create", approval_gate_id=gid,
        )
    assert result["success"] is True
    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT action, target FROM audit_log WHERE agent_name = 't_create' "
                "AND action = 'sf_create' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "sf_create"
    assert "Account" in row[1]


def test_update_record_writes_audit_after_success():
    from shared import governance
    from sqlalchemy import text as sql_text
    from shared.db.connection import get_engine

    gid = governance.create_approval_gate(
        agent_name="t", action_type="single_record_update", payload={}, justification=None
    )
    governance.decide_approval_gate(gid, approved=True, approver="UT")
    with patch.object(salesforce_mcp, "_sf", return_value={"id": "001ABC", "success": True}):
        result = salesforce_mcp.update_record(
            "Account", "001ABC", {"Phone": "999"},
            agent_name="t_update", approval_gate_id=gid,
        )
    assert result["success"] is True
    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT action, target FROM audit_log WHERE agent_name = 't_update' "
                "AND action = 'sf_update' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row[0] == "sf_update"
    assert "001ABC" in row[1]


# ---------------------------------------------------- Smoke entrypoint

def test_smoke_runs_without_crash():
    with patch.object(salesforce_mcp, "list_users", return_value=[{"Id": "1"}, {"Id": "2"}]):
        salesforce_mcp._smoke()  # just must not raise


# ---------------------------------------------------- Metadata deploy thin wrapper

def test_deploy_metadata_uses_source_dir_when_no_manifest():
    with patch.object(salesforce_mcp, "_sf", return_value={"status": "Succeeded"}) as mock_sf:
        salesforce_mcp.deploy_metadata("/tmp/dx-project/src", check_only=True)
    args = mock_sf.call_args.args
    assert "project" in args and "deploy" in args and "start" in args
    assert "--source-dir" in args
    assert "--dry-run" in args


def test_deploy_metadata_uses_manifest_when_provided():
    with patch.object(salesforce_mcp, "_sf", return_value={"status": "Succeeded"}) as mock_sf:
        salesforce_mcp.deploy_metadata(
            "/tmp/dx-project/src", manifest="/tmp/package.xml", test_level="RunLocalTests"
        )
    args = mock_sf.call_args.args
    assert "--manifest" in args and "/tmp/package.xml" in args
    assert "--source-dir" not in args
    assert "--test-level" in args and "RunLocalTests" in args


def test_retrieve_metadata_passes_each_metadata_type():
    with patch.object(salesforce_mcp, "_sf", return_value={"status": "Succeeded"}) as mock_sf:
        salesforce_mcp.retrieve_metadata(
            ["CustomObject:Account", "ApexClass:MyClass"], target_dir="/tmp/out"
        )
    args = mock_sf.call_args.args
    assert "--target-metadata-dir" in args
    # --metadata appears twice, once per type
    assert args.count("--metadata") == 2
