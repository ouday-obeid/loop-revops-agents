"""Propose an SF schema change: emit force-app tree + change.yaml + approval gate.

Entry point for `agents.revops_support.schema.metadata_deployer`. The caller
passes a structured intent; this module writes a self-contained deploy bundle
under `pending_changes/<slug>/` AND opens the appropriate governance gate. The
sandbox tester + prod deployer read the same bundle by slug — files are the
hand-off, not in-memory state.

Intent shape (v1 supports CustomField create/modify/delete only — 90% of
requests; ValidationRule/Layout can be layered in later without changing the
bundle contract):

    {
        "action": "create" | "modify" | "delete",
        "object": "Account",
        "field": {
            "name": "Churn_Risk__c",
            "type": "Number",
            "label": "Churn Risk",
            "length": 3,               # text/number scale — optional per type
            "precision": 3, "scale": 0,
            "description": "...",
        }
    }

Approval tier is selected from the action: `sf_schema_create`,
`sf_schema_modify`, `sf_schema_delete`. Delete uses `dual_approval_cooldown`
(24h) — the existing `schema.cooldown_poller` picks up `approved_primary`
rows and creates the confirmation gate. All three require justification.

Rate limit: `revops_schema_changes_weekly` (soft, warn-only). A schema-change
spike is notable but should not block; `schema.bulk_updater` already provides
the hard-cap for per-row writes.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field as _field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from shared.governance import (
    APPROVAL_TIERS,
    ApprovalRequired,
    check_rate_limit,
    create_approval_gate,
)

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"
RATE_BUCKET = "revops_schema_changes_weekly"

_ACTION_TO_TIER: dict[str, str] = {
    "create": "sf_schema_create",
    "modify": "sf_schema_modify",
    "delete": "sf_schema_delete",
}

_FIELD_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*__c$")
_OBJECT_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(__c)?$")


class ChangeProposalError(ValueError):
    """Invalid intent — caught at proposal time before any side effects."""


@dataclass
class ProposedChange:
    slug: str
    path: Path
    action: str
    sobject: str
    field_name: str
    approval_gate_id: int
    action_type: str
    files: list[str] = _field(default_factory=list)

    def to_summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "path": str(self.path),
            "action": self.action,
            "object": self.sobject,
            "field": self.field_name,
            "approval_gate_id": self.approval_gate_id,
            "action_type": self.action_type,
        }


def _repo_root() -> Path:
    return Path(os.environ.get("REVOPS_REPO_ROOT") or Path(__file__).resolve().parents[3])


def _pending_dir() -> Path:
    root = _repo_root() / "agents" / "revops_support" / "pending_changes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _validate_intent(intent: dict[str, Any]) -> None:
    action = intent.get("action")
    if action not in _ACTION_TO_TIER:
        raise ChangeProposalError(
            f"intent.action must be one of {sorted(_ACTION_TO_TIER)}; got {action!r}"
        )
    obj = intent.get("object")
    if not obj or not _OBJECT_NAME_RE.match(obj):
        raise ChangeProposalError(f"intent.object missing or invalid: {obj!r}")

    fld = intent.get("field") or {}
    name = fld.get("name")
    if not name or not _FIELD_NAME_RE.match(name):
        raise ChangeProposalError(
            f"intent.field.name must match CustomField__c pattern; got {name!r}"
        )
    if action in ("create", "modify"):
        if not fld.get("type"):
            raise ChangeProposalError("intent.field.type required for create/modify")
        if not fld.get("label"):
            raise ChangeProposalError("intent.field.label required for create/modify")


def _slugify(action: str, sobject: str, field_name: str, now: datetime) -> str:
    stamp = now.strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{action}-{sobject}-{field_name}".lower()


def _field_meta_xml(field: dict[str, Any]) -> str:
    name = field["name"]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">',
        f"    <fullName>{name}</fullName>",
    ]
    ftype = field.get("type")
    if ftype:
        lines.append(f"    <type>{ftype}</type>")
    if field.get("label"):
        lines.append(f"    <label>{field['label']}</label>")
    if field.get("description"):
        lines.append(f"    <description>{field['description']}</description>")
    if field.get("length") is not None:
        lines.append(f"    <length>{field['length']}</length>")
    if field.get("precision") is not None:
        lines.append(f"    <precision>{field['precision']}</precision>")
    if field.get("scale") is not None:
        lines.append(f"    <scale>{field['scale']}</scale>")
    if field.get("required"):
        lines.append("    <required>true</required>")
    lines.append("</CustomField>")
    return "\n".join(lines) + "\n"


def _destructive_xml(sobject: str, field_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <types>\n"
        f"        <members>{sobject}.{field_name}</members>\n"
        "        <name>CustomField</name>\n"
        "    </types>\n"
        "    <version>59.0</version>\n"
        "</Package>\n"
    )


def _package_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <types>\n"
        "        <members>*</members>\n"
        "        <name>CustomField</name>\n"
        "    </types>\n"
        "    <version>59.0</version>\n"
        "</Package>\n"
    )


def _write_bundle(
    slug: str, intent: dict[str, Any]
) -> tuple[Path, list[str]]:
    bundle = _pending_dir() / slug
    if bundle.exists():
        raise ChangeProposalError(f"bundle already exists: {bundle}")
    bundle.mkdir(parents=True)
    files: list[str] = []

    action = intent["action"]
    sobject = intent["object"]
    fld = intent["field"]
    field_name = fld["name"]
    force_app = bundle / "force-app" / "main" / "default"

    if action in ("create", "modify"):
        fields_dir = force_app / "objects" / sobject / "fields"
        fields_dir.mkdir(parents=True)
        meta_path = fields_dir / f"{field_name}.field-meta.xml"
        meta_path.write_text(_field_meta_xml(fld))
        files.append(str(meta_path.relative_to(bundle)))

        pkg_path = force_app / "package.xml"
        pkg_path.write_text(_package_xml())
        files.append(str(pkg_path.relative_to(bundle)))
    else:  # delete
        force_app.mkdir(parents=True)
        destructive = force_app / "destructiveChanges.xml"
        destructive.write_text(_destructive_xml(sobject, field_name))
        files.append(str(destructive.relative_to(bundle)))
        pkg_path = force_app / "package.xml"
        pkg_path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <version>59.0</version>\n"
            "</Package>\n"
        )
        files.append(str(pkg_path.relative_to(bundle)))

    return bundle, files


def _write_manifest(
    bundle: Path,
    *,
    slug: str,
    intent: dict[str, Any],
    action_type: str,
    approval_gate_id: int,
    justification: str,
    requested_by: str,
    files: list[str],
    now: datetime,
) -> Path:
    manifest = {
        "slug": slug,
        "action": intent["action"],
        "action_type": action_type,
        "object": intent["object"],
        "field": intent["field"],
        "approval_gate_id": approval_gate_id,
        "justification": justification,
        "requested_by": requested_by,
        "created_at": now.isoformat(),
        "force_app_root": "force-app/main/default",
        "files": files,
        "status": "proposed",
    }
    path = bundle / "change.yaml"
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return path


def propose_change(
    intent: dict[str, Any],
    *,
    justification: str,
    requested_by: str = "system",
    now: datetime | None = None,
) -> ProposedChange:
    """Validate intent → write bundle → open approval gate. Idempotent-per-slug.

    On any failure after bundle creation, the caller is expected to re-run
    with a different timestamp; we do not reuse slugs.
    """
    _validate_intent(intent)
    if not justification or not justification.strip():
        raise ApprovalRequired("schema change requires written justification")

    action = intent["action"]
    action_type = _ACTION_TO_TIER[action]
    if action_type not in APPROVAL_TIERS:
        raise ChangeProposalError(f"governance missing tier for {action_type}")

    now = now or datetime.now(timezone.utc)
    # Soft-limit: schema-change velocity. Warn-only so a backlog day doesn't
    # block execution; the weekly report surfaces the breach.
    check_rate_limit(RATE_BUCKET, window_seconds=604800)

    sobject = intent["object"]
    field_name = intent["field"]["name"]
    slug = _slugify(action, sobject, field_name, now)

    bundle, files = _write_bundle(slug, intent)

    gate_payload = {
        "slug": slug,
        "bundle_path": str(bundle),
        "action": action,
        "object": sobject,
        "field": field_name,
    }
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type=action_type,
        payload=gate_payload,
        justification=justification,
        requested_by=requested_by,
    )

    manifest_path = _write_manifest(
        bundle,
        slug=slug,
        intent=intent,
        action_type=action_type,
        approval_gate_id=gate_id,
        justification=justification,
        requested_by=requested_by,
        files=files,
        now=now,
    )
    files.append(str(manifest_path.relative_to(bundle)))

    log.info(
        "schema change proposed slug=%s action=%s gate=%s",
        slug, action, gate_id,
    )
    return ProposedChange(
        slug=slug,
        path=bundle,
        action=action,
        sobject=sobject,
        field_name=field_name,
        approval_gate_id=gate_id,
        action_type=action_type,
        files=files,
    )


def load_proposal(slug: str) -> dict[str, Any]:
    """Read a bundle's change.yaml back — used by sandbox_tester / deployer."""
    path = _pending_dir() / slug / "change.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no proposal at {path}")
    return yaml.safe_load(path.read_text())
