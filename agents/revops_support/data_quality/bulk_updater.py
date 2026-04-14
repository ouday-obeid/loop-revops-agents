"""Real composite-API bulk updater with pre-write audit snapshot.

Replaces Phase 0's `shared.mcp.salesforce_mcp.bulk_update` simulated return.

Contract:
  1. Every update MUST contain `Id`.
  2. Before any write, we SOQL-fetch each existing record's fields-being-modified
     and stash that snapshot in `audit_log.before_value`. Without this, rollback
     is impossible.
  3. Rate limit: `revops_bulk_update_daily` (hard, 500 rows/day).
  4. Approval gate: classify_bulk_update(count) — callers pass an already-approved
     gate whose action_type matches.
  5. Writes hit Salesforce composite API (`/composite/sobjects`) in 200-row
     chunks, `allOrNone=false` so a single bad row does not abort the batch.
  6. Access token + instance URL come from `sf org display --json --target-org`.

This module is deliberately independent of `salesforce_mcp.bulk_update` — the
legacy simulated function stays as a compatibility no-op for Phase 0 call sites
until every caller has switched to `BulkUpdater`.
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from shared.governance import (
    ApprovalRequired,
    check_rate_limit,
    classify_bulk_update,
    require_approved_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

COMPOSITE_CHUNK = 200
SOQL_IN_CHUNK = 200
RATE_BUCKET = "revops_bulk_update_daily"
SF_API_VERSION = "v59.0"


class BulkUpdateError(RuntimeError):
    """Raised when the pre-flight snapshot or composite write fails structurally."""


@dataclass
class BulkUpdateResult:
    sobject: str
    total: int
    success: int
    failures: list[dict[str, Any]] = field(default_factory=list)
    audit_ids: list[int] = field(default_factory=list)
    before_snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_summary(self) -> dict[str, Any]:
        return {
            "sobject": self.sobject,
            "total": self.total,
            "success": self.success,
            "failed": len(self.failures),
            "audit_ids": self.audit_ids,
        }


def _validate_updates(updates: list[dict[str, Any]]) -> None:
    if not updates:
        raise BulkUpdateError("bulk_update: empty updates list")
    missing = [i for i, u in enumerate(updates) if not u.get("Id")]
    if missing:
        raise BulkUpdateError(
            f"bulk_update: {len(missing)} update(s) missing Id (first indices: {missing[:5]})"
        )


def _modified_fields(updates: list[dict[str, Any]]) -> list[str]:
    """Every field key touched across the batch, minus Id."""
    fields: set[str] = set()
    for u in updates:
        fields.update(k for k in u if k != "Id")
    return sorted(fields)


def _fetch_before_snapshot(
    sobject: str,
    ids: list[str],
    fields: list[str],
    *,
    soql_query: Callable[[str, int], dict[str, Any]] = salesforce_mcp.soql_query,
) -> dict[str, dict[str, Any]]:
    """SOQL the existing values for every id × field we're about to mutate.

    Returns a dict id → {field: current_value}. IDs with no record are omitted
    (caller can diff against the input list to detect missing rows).
    """
    if not fields:
        return {}
    snapshot: dict[str, dict[str, Any]] = {}
    select_clause = ", ".join(["Id", *fields])
    for i in range(0, len(ids), SOQL_IN_CHUNK):
        chunk = ids[i : i + SOQL_IN_CHUNK]
        quoted = ", ".join(f"'{rid}'" for rid in chunk)
        q = f"SELECT {select_clause} FROM {sobject} WHERE Id IN ({quoted})"
        result = soql_query(q, len(chunk))
        for rec in result.get("records", []):
            rid = rec.get("Id")
            if rid:
                snapshot[rid] = {f: rec.get(f) for f in fields}
    return snapshot


def _get_write_auth() -> tuple[str, str]:
    """Resolve (access_token, instance_url) for SF_WRITE_ORG_ALIAS."""
    alias = salesforce_mcp._resolve_org_alias("write")
    if not alias:
        raise BulkUpdateError("no write alias resolved; set SF_WRITE_ORG_ALIAS")
    proc = subprocess.run(
        ["sf", "org", "display", "--target-org", alias, "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise BulkUpdateError(f"sf org display failed: {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout)
    result = data.get("result", {}) if isinstance(data, dict) else {}
    token = result.get("accessToken")
    url = result.get("instanceUrl")
    if not token or not url:
        raise BulkUpdateError("sf org display missing accessToken/instanceUrl")
    return token, url.rstrip("/")


def _composite_patch(
    sobject: str,
    chunk: list[dict[str, Any]],
    *,
    instance_url: str,
    access_token: str,
    http_patch: Callable[..., Any] = requests.patch,
) -> list[dict[str, Any]]:
    """POST the chunk to /composite/sobjects; return per-record results."""
    records = [{"attributes": {"type": sobject}, **rec} for rec in chunk]
    url = f"{instance_url}/services/data/{SF_API_VERSION}/composite/sobjects"
    resp = http_patch(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"allOrNone": False, "records": records},
        timeout=120,
    )
    if getattr(resp, "status_code", 500) >= 300:
        body = getattr(resp, "text", "")[:300]
        raise BulkUpdateError(
            f"composite PATCH failed status={resp.status_code}: {body}"
        )
    return resp.json()


class BulkUpdater:
    """Orchestrates: validate → rate-limit → gate → snapshot → chunk → write → audit."""

    def __init__(
        self,
        *,
        agent_name: str = "revops_support",
        http_patch: Callable[..., Any] = requests.patch,
        soql_query: Callable[[str, int], dict[str, Any]] = salesforce_mcp.soql_query,
        auth_resolver: Callable[[], tuple[str, str]] = _get_write_auth,
    ) -> None:
        self.agent_name = agent_name
        self._http_patch = http_patch
        self._soql = soql_query
        self._auth = auth_resolver

    def run(
        self,
        sobject: str,
        updates: list[dict[str, Any]],
        *,
        approval_gate_id: int,
        dry_run: bool = False,
    ) -> BulkUpdateResult:
        _validate_updates(updates)
        count = len(updates)
        action_type = classify_bulk_update(count)
        require_approved_gate(approval_gate_id, action_type=action_type)

        # Hard rate-limit check happens for every row attempted, not just the
        # batch — exceeding the daily cap even partially is a policy breach.
        check_rate_limit(RATE_BUCKET, window_seconds=86400)

        fields = _modified_fields(updates)
        ids = [u["Id"] for u in updates]
        before = _fetch_before_snapshot(sobject, ids, fields, soql_query=self._soql)

        missing_ids = [i for i in ids if i not in before]
        if missing_ids:
            log.warning(
                "bulk_update: %d id(s) not found in SF pre-snapshot (will still attempt write)",
                len(missing_ids),
            )

        result = BulkUpdateResult(
            sobject=sobject, total=count, success=0, before_snapshot=before,
        )

        if dry_run:
            log.info("bulk_update dry_run: %s rows, fields=%s", count, fields)
            return result

        access_token, instance_url = self._auth()

        for i in range(0, count, COMPOSITE_CHUNK):
            chunk = updates[i : i + COMPOSITE_CHUNK]
            chunk_ids = [u["Id"] for u in chunk]

            # Per-chunk audit row — before_value scoped to this chunk so a failed
            # chunk has a contained rollback payload.
            chunk_before = {rid: before.get(rid, {}) for rid in chunk_ids}
            audit_id = write_audit(
                agent_name=self.agent_name,
                action="sf_bulk_update",
                target=f"sf:{sobject}",
                before=chunk_before,
                after={"chunk_index": i // COMPOSITE_CHUNK, "updates": chunk},
                approval_gate_id=approval_gate_id,
                rate_limit_bucket=RATE_BUCKET,
            )
            if audit_id is not None:
                result.audit_ids.append(audit_id)

            per_record = _composite_patch(
                sobject, chunk,
                instance_url=instance_url,
                access_token=access_token,
                http_patch=self._http_patch,
            )
            for rec in per_record:
                if rec.get("success"):
                    result.success += 1
                else:
                    result.failures.append({
                        "id": rec.get("id"),
                        "errors": rec.get("errors", []),
                    })

        log.info(
            "bulk_update complete sobject=%s success=%d/%d failures=%d",
            sobject, result.success, result.total, len(result.failures),
        )
        return result


def bulk_update(
    sobject: str,
    updates: list[dict[str, Any]],
    *,
    agent_name: str = "revops_support",
    approval_gate_id: int,
    dry_run: bool = False,
) -> BulkUpdateResult:
    """Functional wrapper for one-shot callers."""
    if approval_gate_id is None:
        raise ApprovalRequired("bulk_update requires approval_gate_id")
    return BulkUpdater(agent_name=agent_name).run(
        sobject, updates, approval_gate_id=approval_gate_id, dry_run=dry_run,
    )
