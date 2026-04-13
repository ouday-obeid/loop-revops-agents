"""Deploy an approved schema change to prod, with a pre-deploy revert snapshot.

Runs AFTER the gate transitions to `approved` (or, for deletes, after the
confirmation gate flips `approved`). Before issuing the prod deploy we:

  1. Verify gate status matches the action (strict — a delete bundle without
     an approved `sf_schema_delete_confirm` child is rejected).
  2. Verify the sandbox test in change.yaml was `passed`.
  3. Check the daily deploy rate limit (`revops_metadata_deploy_daily`, 5/day).
  4. For modify/delete: retrieve the current metadata via `sf project retrieve
     start` and stash under bundle/revert/force-app — this is what rollback
     replays on failure.
  5. Generate a revert_package.xml (destructive for create, re-deploy for
     modify/delete) alongside the revert snapshot.
  6. Invoke `deploy_metadata(intent="write")`.
  7. Write audit (before=manifest snapshot, after=deploy response) and stamp
     change.yaml with status=deployed + deploy_id.

The caller is expected to wrap deploy() in a try/except and pass failures to
`schema.rollback.rollback(slug)`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml
from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import (
    ApprovalRequired,
    check_rate_limit,
    require_approved_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp

from .change_proposer import _pending_dir

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"
RATE_BUCKET = "revops_metadata_deploy_daily"


class DeployPreconditionError(RuntimeError):
    """Gate/test/precondition violation — raised before any SF call."""


@dataclass
class DeployResult:
    slug: str
    deploy_id: str | None
    success: bool
    revert_dir: Path | None = None
    audit_id: int | None = None
    error_message: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _bundle_dir(slug: str) -> Path:
    return _pending_dir() / slug


def _load_manifest(slug: str) -> dict[str, Any]:
    path = _bundle_dir(slug) / "change.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no proposal at {path}")
    return yaml.safe_load(path.read_text())


def _save_manifest(slug: str, manifest: dict[str, Any]) -> None:
    path = _bundle_dir(slug) / "change.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def _delete_confirm_gate(primary_gate_id: int) -> dict[str, Any] | None:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT id, status FROM approval_gates
                    WHERE parent_gate_id = :p
                      AND action_type = 'sf_schema_delete_confirm'
                    ORDER BY id DESC LIMIT 1"""
            ),
            {"p": primary_gate_id},
        ).mappings().fetchone()
    return dict(row) if row else None


def _verify_gate(manifest: dict[str, Any]) -> None:
    """Enforce the right gate state for the action.

    create/modify → gate.status == 'approved'.
    delete → parent gate.status ∈ {'approved_primary','approved'} AND an
    `approved` confirmation child exists (see `schema.cooldown_poller`).
    """
    gate_id = manifest.get("approval_gate_id")
    if not gate_id:
        raise DeployPreconditionError("manifest missing approval_gate_id")
    action_type = manifest["action_type"]

    if action_type in ("sf_schema_create", "sf_schema_modify"):
        require_approved_gate(gate_id, action_type=action_type)
        return

    # Delete: require a confirmed child gate.
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM approval_gates WHERE id = :i"), {"i": gate_id}
        ).fetchone()
    if row is None:
        raise DeployPreconditionError(f"gate {gate_id} not found")
    if row[0] not in ("approved_primary", "approved"):
        raise ApprovalRequired(
            f"delete parent gate {gate_id} status={row[0]}; expected approved_primary"
        )
    child = _delete_confirm_gate(gate_id)
    if not child or child["status"] != "approved":
        raise ApprovalRequired(
            f"delete gate {gate_id} missing approved confirmation child"
        )


def _verify_sandbox_passed(manifest: dict[str, Any]) -> None:
    sandbox = manifest.get("sandbox_test") or {}
    if sandbox.get("status") != "passed":
        raise DeployPreconditionError(
            f"sandbox_test.status={sandbox.get('status')!r}; expected 'passed'"
        )


def _retrieve_pre_snapshot(
    manifest: dict[str, Any],
    target_dir: Path,
    *,
    retrieve_fn: Callable[..., dict[str, Any]] = salesforce_mcp.retrieve_metadata,
) -> None:
    """For modify/delete, snapshot the live metadata before we mutate it.

    Stored under bundle/revert/force-app/. Create actions do NOT need a
    pre-snapshot — their revert is a destructive redeploy of the new field.
    """
    if manifest["action"] == "create":
        return
    target_dir.mkdir(parents=True, exist_ok=True)
    metadata_item = f"CustomField:{manifest['object']}.{manifest['field']['name']}"
    retrieve_fn(metadata_item, target_dir=str(target_dir), intent="write")


def _build_revert_package(manifest: dict[str, Any], revert_root: Path) -> None:
    """Write revert/package.xml describing how to undo this change."""
    revert_root.mkdir(parents=True, exist_ok=True)
    action = manifest["action"]
    sobject = manifest["object"]
    field_name = manifest["field"]["name"]

    if action == "create":
        (revert_root / "destructiveChanges.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <types>\n"
            f"        <members>{sobject}.{field_name}</members>\n"
            "        <name>CustomField</name>\n"
            "    </types>\n"
            "    <version>59.0</version>\n"
            "</Package>\n"
        )
        (revert_root / "package.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <version>59.0</version>\n"
            "</Package>\n"
        )
    else:
        # Re-deploy the pre-change metadata captured under force-app/.
        (revert_root / "package.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <types>\n"
            f"        <members>{sobject}.{field_name}</members>\n"
            "        <name>CustomField</name>\n"
            "    </types>\n"
            "    <version>59.0</version>\n"
            "</Package>\n"
        )


def deploy(
    slug: str,
    *,
    deploy_fn: Callable[..., dict[str, Any]] = salesforce_mcp.deploy_metadata,
    retrieve_fn: Callable[..., dict[str, Any]] = salesforce_mcp.retrieve_metadata,
    now: datetime | None = None,
) -> DeployResult:
    now = now or datetime.now(timezone.utc)
    manifest = _load_manifest(slug)

    _verify_gate(manifest)
    _verify_sandbox_passed(manifest)

    # Hard daily cap — raises if over.
    check_rate_limit(RATE_BUCKET, window_seconds=86400)

    bundle = _bundle_dir(slug)
    revert_root = bundle / "revert"
    revert_force_app = revert_root / "force-app"

    _retrieve_pre_snapshot(manifest, revert_force_app, retrieve_fn=retrieve_fn)
    _build_revert_package(manifest, revert_root)

    source = bundle / "force-app"
    try:
        raw = deploy_fn(str(source), intent="write", check_only=False)
    except Exception as e:  # noqa: BLE001
        manifest["status"] = "deploy_failed"
        manifest["deploy_error"] = str(e)[:500]
        _save_manifest(slug, manifest)
        log.exception("prod deploy failed slug=%s", slug)
        return DeployResult(
            slug=slug, deploy_id=None, success=False,
            revert_dir=revert_root, error_message=str(e)[:500],
        )

    deploy_id = raw.get("id") or raw.get("deployId")
    success = bool(raw.get("success")) and str(raw.get("status", "")).lower() in (
        "succeeded", "succeededpartial", ""
    )

    audit_id = write_audit(
        agent_name=AGENT_NAME,
        action="sf_schema_deploy",
        target=f"sf:{manifest['object']}.{manifest['field']['name']}",
        before={"slug": slug, "pre_deploy_manifest": manifest},
        after={"deploy_id": deploy_id, "success": success, "raw_status": raw.get("status")},
        approval_gate_id=manifest.get("approval_gate_id"),
        rate_limit_bucket=RATE_BUCKET,
    )

    manifest["status"] = "deployed" if success else "deploy_failed"
    manifest["deploy_id"] = deploy_id
    manifest["deployed_at"] = now.isoformat()
    _save_manifest(slug, manifest)

    log.info(
        "prod deploy complete slug=%s success=%s deploy_id=%s",
        slug, success, deploy_id,
    )
    return DeployResult(
        slug=slug, deploy_id=deploy_id, success=success,
        revert_dir=revert_root, audit_id=audit_id, raw=raw,
    )
