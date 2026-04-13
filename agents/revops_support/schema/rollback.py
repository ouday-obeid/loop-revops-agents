"""Roll back a deployed schema change using the pre-deploy revert snapshot.

`metadata_deployer.deploy()` pre-writes `bundle/revert/` — this module
consumes that directory. Rollback is itself a schema change, so it opens a
fresh `sf_schema_modify` gate (or `sf_schema_delete` if the original was a
create, since rollback of a create is a destructive delete). That means
rollback is governed by the same approval primitives as any other change.

Typical use:

    result = metadata_deployer.deploy(slug)
    if not result.success:
        rollback.prepare(slug, justification="<why>", requested_by=...)
        # O approves the rollback gate in Slack
        rollback.execute(slug)

Rollback is idempotent per slug: running twice after success no-ops.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import (
    ApprovalRequired,
    create_approval_gate,
    require_approved_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp

from .change_proposer import _pending_dir

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"


class RollbackError(RuntimeError):
    """Precondition or deploy failure during rollback."""


@dataclass
class RollbackResult:
    slug: str
    gate_id: int | None
    deploy_id: str | None
    success: bool
    error_message: str | None = None


def _bundle_dir(slug: str) -> Path:
    return _pending_dir() / slug


def _load_manifest(slug: str) -> dict[str, Any]:
    path = _bundle_dir(slug) / "change.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no bundle at {path}")
    return yaml.safe_load(path.read_text())


def _save_manifest(slug: str, manifest: dict[str, Any]) -> None:
    path = _bundle_dir(slug) / "change.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def _rollback_action_type(original_action: str) -> str:
    # create  → rollback = destructive delete of the new field
    # modify  → rollback = modify (re-deploy prior snapshot)
    # delete  → rollback = modify (re-provision the pre-delete field)
    return "sf_schema_delete" if original_action == "create" else "sf_schema_modify"


def prepare(
    slug: str,
    *,
    justification: str,
    requested_by: str = "system",
) -> int:
    """Open a rollback approval gate. Returns gate_id."""
    if not justification or not justification.strip():
        raise ApprovalRequired("rollback requires written justification")

    manifest = _load_manifest(slug)
    revert_dir = _bundle_dir(slug) / "revert"
    if not revert_dir.exists():
        raise RollbackError(f"no revert snapshot at {revert_dir}")

    action_type = _rollback_action_type(manifest["action"])
    payload = {
        "slug": slug,
        "original_action": manifest["action"],
        "original_deploy_id": manifest.get("deploy_id"),
        "revert_dir": str(revert_dir),
        "object": manifest["object"],
        "field": manifest["field"]["name"],
        "origin": "revops_support_rollback",
    }
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type=action_type,
        payload=payload,
        justification=justification,
        requested_by=requested_by,
    )

    manifest["rollback_gate_id"] = gate_id
    manifest["rollback_status"] = "pending"
    _save_manifest(slug, manifest)
    log.info("rollback gate opened slug=%s gate=%s", slug, gate_id)
    return gate_id


def _already_rolled_back(manifest: dict[str, Any]) -> bool:
    return manifest.get("rollback_status") == "deployed"


def execute(
    slug: str,
    *,
    deploy_fn: Callable[..., dict[str, Any]] = salesforce_mcp.deploy_metadata,
    now: datetime | None = None,
) -> RollbackResult:
    """Deploy the revert bundle after the rollback gate is approved."""
    now = now or datetime.now(timezone.utc)
    manifest = _load_manifest(slug)

    if _already_rolled_back(manifest):
        log.info("rollback already deployed slug=%s — no-op", slug)
        return RollbackResult(
            slug=slug,
            gate_id=manifest.get("rollback_gate_id"),
            deploy_id=manifest.get("rollback_deploy_id"),
            success=True,
        )

    gate_id = manifest.get("rollback_gate_id")
    if not gate_id:
        raise RollbackError(f"no rollback_gate_id on manifest; run prepare() first")

    action_type = _rollback_action_type(manifest["action"])

    # For rollback of an original delete we'd need a confirmation child gate to
    # re-enable the field-deletion path. V1 scope: rollback is for create/modify
    # only (the live incident pattern). Escalate delete-rollbacks to O manually.
    if manifest["action"] == "delete":
        raise RollbackError(
            "rollback of a delete is not supported in v1 — manual restore required"
        )

    require_approved_gate(gate_id, action_type=action_type)

    revert_source = _bundle_dir(slug) / "revert"
    if not revert_source.exists():
        raise RollbackError(f"revert snapshot missing at {revert_source}")

    try:
        raw = deploy_fn(str(revert_source), intent="write", check_only=False)
    except Exception as e:  # noqa: BLE001
        manifest["rollback_status"] = "failed"
        manifest["rollback_error"] = str(e)[:500]
        _save_manifest(slug, manifest)
        log.exception("rollback deploy failed slug=%s", slug)
        return RollbackResult(
            slug=slug, gate_id=gate_id, deploy_id=None, success=False,
            error_message=str(e)[:500],
        )

    deploy_id = raw.get("id") or raw.get("deployId")
    success = bool(raw.get("success")) and str(raw.get("status", "")).lower() in (
        "succeeded", "succeededpartial", ""
    )

    write_audit(
        agent_name=AGENT_NAME,
        action="sf_schema_rollback",
        target=f"sf:{manifest['object']}.{manifest['field']['name']}",
        before={"slug": slug, "original_deploy_id": manifest.get("deploy_id")},
        after={"deploy_id": deploy_id, "success": success, "raw_status": raw.get("status")},
        approval_gate_id=gate_id,
    )

    manifest["rollback_status"] = "deployed" if success else "failed"
    manifest["rollback_deploy_id"] = deploy_id
    manifest["rolled_back_at"] = now.isoformat()
    _save_manifest(slug, manifest)

    return RollbackResult(
        slug=slug, gate_id=gate_id, deploy_id=deploy_id, success=success,
    )
