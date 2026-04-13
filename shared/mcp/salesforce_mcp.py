"""Salesforce MCP — reads via `sf` CLI; writes gated by governance.

Phase 0 surface:
  - soql_query, describe_sobject, get_record, list_users  (read)
  - create_record, update_record                          (write, logged)
  - bulk_update                                           (write, approval-gated)
  - describe_flow                                         (tooling read)

Phase 1 additions:
  - _sf() gains an `intent` kwarg ("read" | "write" | "sandbox") to select the
    service-user alias. Reads use SF_ORG_ALIAS; writes use SF_WRITE_ORG_ALIAS
    (falls back to SF_ORG_ALIAS); sandbox ops use SF_SANDBOX_ORG_ALIAS.
  - deploy_metadata / retrieve_metadata wrappers over `sf project deploy|retrieve`.

All writes require an approved approval_gate_id. bulk_update with count >= 100
rejects BEFORE any sf CLI invocation.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from typing import Any, Literal

from shared.governance import (
    ApprovalRequired,
    classify_bulk_update,
    require_approved_gate,
    write_audit,
)
from shared.secrets import get_config

log = logging.getLogger(__name__)

_SF_BIN = shutil.which("sf") or "sf"

Intent = Literal["read", "write", "sandbox"]


class SalesforceError(RuntimeError):
    pass


def _resolve_org_alias(intent: Intent) -> str:
    """Pick the sf target-org alias for the given intent.

    Write falls back to read so Phase 0 deployments (pre service-user split)
    keep working even before SF_WRITE_ORG_ALIAS is configured. Sandbox has no
    fallback — a sandbox op without SF_SANDBOX_ORG_ALIAS is a config error.
    """
    if intent == "read":
        return get_config("SF_ORG_ALIAS") or get_config("SF_SERVICE_USER") or ""
    if intent == "write":
        return (
            get_config("SF_WRITE_ORG_ALIAS")
            or get_config("SF_ORG_ALIAS")
            or get_config("SF_SERVICE_USER")
            or ""
        )
    if intent == "sandbox":
        alias = get_config("SF_SANDBOX_ORG_ALIAS")
        if not alias:
            raise SalesforceError(
                "SF_SANDBOX_ORG_ALIAS not configured; sandbox intent requires explicit alias"
            )
        return alias
    raise ValueError(f"unknown intent: {intent}")


def _sf(
    *args: str,
    json_out: bool = True,
    intent: Intent = "read",
    timeout: int = 60,
    cwd: str | None = None,
) -> dict[str, Any]:
    org = _resolve_org_alias(intent)
    cmd = [_SF_BIN, *args]
    if json_out:
        cmd.append("--json")
    if org and "--target-org" not in args and "-o" not in args:
        cmd.extend(["--target-org", org])
    log.debug("sf cmd (intent=%s cwd=%s): %s", intent, cwd, " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if not json_out:
        if proc.returncode != 0:
            raise SalesforceError(proc.stderr.strip() or proc.stdout.strip())
        return {"stdout": proc.stdout}
    # sf CLI sometimes returns nonzero even on success (update-available warnings).
    # Trust the JSON payload's status field when parseable.
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise SalesforceError(
            f"sf failed rc={proc.returncode}: {(proc.stderr or proc.stdout).strip()[:300]}"
        ) from e
    # sf CLI occasionally returns outer status=1 with a cosmetic locale error
    # ("Missing message ..."); when that happens the inner result still
    # reflects the true outcome. Trust the inner payload when it's present and
    # declares its own success state.
    result = data.get("result")
    inner_status = (result or {}).get("status") if isinstance(result, dict) else None
    inner_success = (result or {}).get("success") if isinstance(result, dict) else None
    if data.get("status") not in (0, None):
        inner_ok = (
            inner_success is True
            or (isinstance(inner_status, str) and inner_status.lower() in ("succeeded", "succeededpartial"))
        )
        if not inner_ok:
            log.warning(
                "sf nonzero outer status; keys=%s inner_status=%r inner_success=%r payload=%s",
                list(data.keys()), inner_status, inner_success, json.dumps(data)[:1500],
            )
            raise SalesforceError(data.get("message") or proc.stdout[:200])
    return result if result is not None else data


# ---------- Reads ----------

_AGG_NO_GROUP_RE = re.compile(
    r"\bSELECT\b.*?\b(?:COUNT\s*\(|SUM\s*\(|MAX\s*\(|MIN\s*\(|AVG\s*\()",
    re.IGNORECASE | re.DOTALL,
)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def _needs_limit(query: str) -> bool:
    """Skip LIMIT on aggregate queries without GROUP BY — SF rejects those."""
    if "limit" in query.lower():
        return False
    if _AGG_NO_GROUP_RE.search(query) and not _GROUP_BY_RE.search(query):
        return False
    return True


def soql_query(query: str, limit: int = 100, *, intent: Intent = "read") -> dict[str, Any]:
    q = f"{query.rstrip(';')} LIMIT {limit}" if _needs_limit(query) else query
    return _sf("data", "query", "--query", q, intent=intent)


def describe_sobject(name: str, *, intent: Intent = "read") -> dict[str, Any]:
    return _sf("sobject", "describe", "--sobject", name, intent=intent)


def get_record(sobject: str, record_id: str, *, intent: Intent = "read") -> dict[str, Any]:
    return _sf(
        "data", "get", "record", "--sobject", sobject, "--record-id", record_id,
        intent=intent,
    )


def list_users(active_only: bool = True, *, intent: Intent = "read") -> list[dict[str, Any]]:
    q = "SELECT Id, Name, Username, Email, IsActive, UserRoleId FROM User"
    if active_only:
        q += " WHERE IsActive = true"
    result = soql_query(q, limit=1000, intent=intent)
    return result.get("records", [])


def describe_flow(flow_id: str, *, intent: Intent = "read") -> dict[str, Any]:
    q = f"SELECT Id, MasterLabel, Status, ProcessType FROM Flow WHERE Id = '{flow_id}'"
    return _sf("data", "query", "--query", q, "--use-tooling-api", intent=intent)


def tooling_query(
    query: str, limit: int | None = None, *, intent: Intent = "read"
) -> dict[str, Any]:
    """Tooling API SOQL. Required for ValidationRule, Flow, ApexClass, etc."""
    if limit is None or not _needs_limit(query):
        q = query
    else:
        q = f"{query.rstrip(';')} LIMIT {limit}"
    return _sf("data", "query", "--query", q, "--use-tooling-api", intent=intent)


# ---------- Writes ----------

def create_record(
    sobject: str,
    fields: dict[str, Any],
    *,
    agent_name: str,
    approval_gate_id: int | None = None,
    intent: Intent = "write",
) -> dict[str, Any]:
    require_approved_gate(approval_gate_id, action_type="single_record_update")
    values = " ".join(f"{k}={json.dumps(v)}" for k, v in fields.items())
    result = _sf(
        "data", "create", "record", "--sobject", sobject, "--values", values,
        intent=intent,
    )
    write_audit(
        agent_name=agent_name,
        action="sf_create",
        target=f"sf:{sobject}",
        after=fields,
        approval_gate_id=approval_gate_id,
    )
    return result


def update_record(
    sobject: str,
    record_id: str,
    fields: dict[str, Any],
    *,
    agent_name: str,
    approval_gate_id: int | None = None,
    intent: Intent = "write",
) -> dict[str, Any]:
    require_approved_gate(approval_gate_id, action_type="single_record_update")
    values = " ".join(f"{k}={json.dumps(v)}" for k, v in fields.items())
    result = _sf(
        "data", "update", "record",
        "--sobject", sobject,
        "--record-id", record_id,
        "--values", values,
        intent=intent,
    )
    write_audit(
        agent_name=agent_name,
        action="sf_update",
        target=f"sf:{sobject}:{record_id}",
        after=fields,
        approval_gate_id=approval_gate_id,
    )
    return result


def bulk_update(
    sobject: str,
    updates: list[dict[str, Any]],
    *,
    agent_name: str,
    approval_gate_id: int,
) -> dict[str, Any]:
    count = len(updates)
    action_type = classify_bulk_update(count)
    # Enforce BEFORE any sf invocation
    if count >= 100 and approval_gate_id is None:
        raise ApprovalRequired(
            f"bulk_update of {count} records requires approval_gate_id (>=100)"
        )
    require_approved_gate(approval_gate_id, action_type=action_type)
    write_audit(
        agent_name=agent_name,
        action="sf_bulk_update",
        target=f"sf:{sobject}",
        after={"count": count, "sample": updates[:3]},
        approval_gate_id=approval_gate_id,
    )
    # Phase 0: do not actually execute bulk writes against production.
    return {"simulated": True, "count": count, "sobject": sobject}


# ---------- Metadata deploy/retrieve (Phase 1 schema path) ----------

def deploy_metadata(
    source_dir: str,
    *,
    intent: Intent = "sandbox",
    check_only: bool = False,
    test_level: str | None = None,
    timeout: int = 1800,
) -> dict[str, Any]:
    """Wraps `sf project deploy start`.

    Default intent is sandbox — prod deploys must pass intent="write" AFTER an
    approved sf_schema_* gate; the caller (schema/metadata_deployer.py) enforces.
    test_level: None | NoTestRun | RunLocalTests | RunAllTestsInOrg.
    """
    args = ["project", "deploy", "start", "--source-dir", source_dir]
    if check_only:
        args.append("--dry-run")
    if test_level:
        args.extend(["--test-level", test_level])
    # `sf project deploy start` requires cwd to contain an sfdx-project.json;
    # callers pass a bundle dir that already satisfies this (or resolve to a
    # parent repo with the right layout).
    import os.path as _op
    cwd = _op.dirname(source_dir.rstrip("/")) if source_dir else None
    return _sf(*args, intent=intent, timeout=timeout, cwd=cwd)


def retrieve_metadata(
    metadata: str | list[str],
    *,
    target_dir: str,
    intent: Intent = "read",
    timeout: int = 1800,
) -> dict[str, Any]:
    """Wraps `sf project retrieve start --metadata`.

    `metadata` accepts e.g. "CustomObject:Account" or a list for multiple types.
    """
    types = metadata if isinstance(metadata, list) else [metadata]
    args = ["project", "retrieve", "start", "--target-metadata-dir", target_dir]
    for t in types:
        args.extend(["--metadata", t])
    return _sf(*args, intent=intent, timeout=timeout)


# ---------- Smoke test entrypoint ----------

def _smoke() -> None:
    users = list_users(active_only=True)
    print(f"active users: {len(users)}")


if __name__ == "__main__":
    import sys
    if "--smoke" in sys.argv:
        _smoke()
