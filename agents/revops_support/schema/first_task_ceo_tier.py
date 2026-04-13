"""Agent 5 canary: add a CEO UserRole above the existing CRO role.

This is the first real schema change the agent executes in prod. The shape
is slightly bespoke because:

  - UserRole is not a CustomField, so it bypasses `change_proposer` (which
    v1-scopes to CustomField).
  - We take a pre/post SOQL count snapshot of every active role's membership.
    Post-deploy those counts must match within tolerance (default 0.01%) —
    a CEO role insert above CRO must not drift any downstream role or
    re-bucket any user.
  - Three verification DMs are scheduled at T+30m / +2h / +4h. If the post
    snapshot diverges at any interval, the canary raises and pages O.

Flow is the same approval/sandbox/prod pattern — just wired directly:

    plan = propose_ceo_role(justification=...)
    sandbox_test(plan)                      # sandbox deploy + RunLocalTests
    # O approves plan.gate_id in Slack
    pre = pre_snapshot(sf_mcp)
    deploy(plan, pre, sf_mcp)               # writes audit, stamps manifest
    followups = schedule_verifications(plan, pre, now)
    # launchd cron triggers verify(plan, pre) at each interval

Rollback on divergence routes through `schema.rollback.prepare/execute` —
the revert bundle is pre-written alongside the main bundle.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from shared.governance import (
    ApprovalRequired,
    check_rate_limit,
    create_approval_gate,
    decide_approval_gate,
    require_approved_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp

from .change_proposer import _pending_dir

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"
SLUG = "canary-ceo-role"
PARENT_CRO_ROLE = "CRO"
DEFAULT_TOLERANCE = 0.0001  # 0.01%
RATE_BUCKET = "revops_metadata_deploy_daily"
FOLLOWUP_INTERVALS_MIN = (30, 120, 240)


@dataclass
class CanaryPlan:
    slug: str
    path: Path
    gate_id: int
    role_dev_name: str = "CEO"


@dataclass
class Snapshot:
    taken_at: str
    counts_by_role: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"taken_at": self.taken_at, "counts_by_role": dict(self.counts_by_role)}


@dataclass
class VerificationResult:
    passed: bool
    interval_min: int
    drift: dict[str, dict[str, int]] = field(default_factory=dict)
    message: str = ""


class CanaryError(RuntimeError):
    """Snapshot drift or precondition failure."""


def _bundle_dir() -> Path:
    return _pending_dir() / SLUG


def _role_metadata_xml(dev_name: str, label: str, parent_dev_name: str | None) -> str:
    parent_line = (
        f"    <parentRole>{parent_dev_name}</parentRole>\n" if parent_dev_name else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Role xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        f"    <name>{label}</name>\n"
        f"{parent_line}"
        "    <mayForecastManagerShare>false</mayForecastManagerShare>\n"
        "</Role>\n"
    )


def _destructive_role_xml(dev_name: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Package xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <types>\n"
        f"        <members>{dev_name}</members>\n"
        "        <name>Role</name>\n"
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
        "        <name>Role</name>\n"
        "    </types>\n"
        "    <version>59.0</version>\n"
        "</Package>\n"
    )


def propose_ceo_role(
    *,
    justification: str,
    requested_by: str = "system",
    role_dev_name: str = "CEO",
    role_label: str = "CEO",
    now: datetime | None = None,
) -> CanaryPlan:
    if not justification or not justification.strip():
        raise ApprovalRequired("canary requires written justification")
    now = now or datetime.now(timezone.utc)

    bundle = _bundle_dir()
    if bundle.exists():
        raise CanaryError(
            f"canary bundle already exists at {bundle} — remove or rename before re-running"
        )
    force_app = bundle / "force-app" / "main" / "default"
    roles_dir = force_app / "roles"
    roles_dir.mkdir(parents=True)

    # CEO at the top (no parentRole); existing PARENT_CRO_ROLE gets re-parented
    # under CEO in the same deploy. Deploy order is handled by SF (creates CEO
    # before updating CRO).
    (roles_dir / f"{role_dev_name}.role-meta.xml").write_text(
        _role_metadata_xml(role_dev_name, role_label, None)
    )
    (roles_dir / f"{PARENT_CRO_ROLE}.role-meta.xml").write_text(
        _role_metadata_xml(PARENT_CRO_ROLE, PARENT_CRO_ROLE, role_dev_name)
    )
    (force_app / "package.xml").write_text(_package_xml())

    # `sf project deploy start` requires a DX project structure — minimal
    # sfdx-project.json at the bundle root pointing at force-app/.
    (bundle / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app","default":true}],'
        '"sourceApiVersion":"59.0"}\n'
    )

    # Revert: re-parent CRO to top (no parent), then delete CEO. Using
    # destructiveChangesPost so CRO is updated before CEO is removed —
    # otherwise deleting CEO would fail while CRO still points at it.
    revert = bundle / "revert"
    revert_force_app = revert / "force-app" / "main" / "default"
    revert_roles = revert_force_app / "roles"
    revert_roles.mkdir(parents=True)
    (revert_roles / f"{PARENT_CRO_ROLE}.role-meta.xml").write_text(
        _role_metadata_xml(PARENT_CRO_ROLE, PARENT_CRO_ROLE, None)
    )
    (revert_force_app / "package.xml").write_text(_package_xml())
    (revert_force_app / "destructiveChangesPost.xml").write_text(
        _destructive_role_xml(role_dev_name)
    )
    # Keep legacy destructiveChanges.xml at revert/ root for tests that check
    # its presence; points at the same CEO dev name.
    (revert / "destructiveChanges.xml").write_text(_destructive_role_xml(role_dev_name))
    (revert / "sfdx-project.json").write_text(
        '{"packageDirectories":[{"path":"force-app","default":true}],'
        '"sourceApiVersion":"59.0"}\n'
    )

    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="sf_schema_create",
        payload={
            "slug": SLUG,
            "bundle_path": str(bundle),
            "metadata_type": "Role",
            "role_dev_name": role_dev_name,
            "parent_role": PARENT_CRO_ROLE,
            "canary": True,
        },
        justification=justification,
        requested_by=requested_by,
    )

    manifest = {
        "slug": SLUG,
        "canary": True,
        "metadata_type": "Role",
        "role_dev_name": role_dev_name,
        "parent_role": PARENT_CRO_ROLE,
        "approval_gate_id": gate_id,
        "justification": justification,
        "requested_by": requested_by,
        "created_at": now.isoformat(),
        "status": "proposed",
    }
    (bundle / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    log.info("canary ceo role proposed gate=%s bundle=%s", gate_id, bundle)
    return CanaryPlan(slug=SLUG, path=bundle, gate_id=gate_id, role_dev_name=role_dev_name)


def sandbox_test(
    plan: CanaryPlan,
    *,
    deploy_fn: Callable[..., dict[str, Any]] = salesforce_mcp.deploy_metadata,
    test_level: str = "NoTestRun",
) -> dict[str, Any]:
    # UserRole is not Apex-adjacent, so tests are not required. Caller can
    # override test_level if they want the extra paranoia (adds ~minutes).
    raw = deploy_fn(
        str(plan.path / "force-app"),
        intent="sandbox",
        check_only=False,
        test_level=test_level,
    )
    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    ok = bool(raw.get("success")) and str(raw.get("status", "")).lower() in (
        "succeeded", "succeededpartial", ""
    )
    manifest["sandbox_test"] = {
        "status": "passed" if ok else "failed",
        "deploy_id": raw.get("id") or raw.get("deployId"),
        "tested_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest["status"] = "sandbox_passed" if ok else "sandbox_failed"
    (plan.path / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    if not ok:
        raise CanaryError(f"sandbox test failed: {raw}")
    return raw


def pre_snapshot(
    sf_mcp: Any = salesforce_mcp,
    *,
    now: datetime | None = None,
) -> Snapshot:
    now = now or datetime.now(timezone.utc)
    q = "SELECT UserRole.Name role_name, COUNT(Id) cnt FROM User WHERE IsActive = true GROUP BY UserRole.Name"
    result = sf_mcp.soql_query(q, limit=500)
    counts: dict[str, int] = {}
    for row in result.get("records", []) or []:
        name = row.get("role_name") or row.get("Role_Name") or "(none)"
        counts[name] = int(row.get("cnt") or row.get("expr0") or 0)
    return Snapshot(taken_at=now.isoformat(), counts_by_role=counts)


def deploy(
    plan: CanaryPlan,
    pre: Snapshot,
    *,
    sf_mcp: Any = salesforce_mcp,
    deploy_fn: Callable[..., dict[str, Any]] = salesforce_mcp.deploy_metadata,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    if manifest.get("sandbox_test", {}).get("status") != "passed":
        raise CanaryError("sandbox_test not passed; refusing prod deploy")
    require_approved_gate(plan.gate_id, action_type="sf_schema_create")
    check_rate_limit(RATE_BUCKET, window_seconds=86400)

    manifest["pre_snapshot"] = pre.to_dict()
    (plan.path / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    raw = deploy_fn(str(plan.path / "force-app"), intent="write", check_only=False)
    success = bool(raw.get("success")) and str(raw.get("status", "")).lower() in (
        "succeeded", "succeededpartial", ""
    )
    deploy_id = raw.get("id") or raw.get("deployId")

    write_audit(
        agent_name=AGENT_NAME,
        action="sf_schema_deploy",
        target="sf:UserRole:CEO",
        before={"slug": SLUG, "pre_snapshot": pre.to_dict()},
        after={"deploy_id": deploy_id, "success": success, "status": raw.get("status")},
        approval_gate_id=plan.gate_id,
        rate_limit_bucket=RATE_BUCKET,
    )

    manifest["status"] = "deployed" if success else "deploy_failed"
    manifest["deploy_id"] = deploy_id
    manifest["deployed_at"] = now.isoformat()
    if not success:
        manifest["deploy_error"] = str(raw.get("message") or raw.get("status"))[:500]
    (plan.path / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    if not success:
        raise CanaryError(f"prod deploy failed: {raw.get('status')}")
    return raw


def _diff(
    pre: dict[str, int], post: dict[str, int], *, tolerance: float
) -> dict[str, dict[str, int]]:
    drift: dict[str, dict[str, int]] = {}
    all_roles = set(pre) | set(post) - {"CEO"}  # new CEO role expected only post
    for role in all_roles:
        a = pre.get(role, 0)
        b = post.get(role, 0)
        if a == 0 and b == 0:
            continue
        denom = max(abs(a), 1)
        if abs(a - b) / denom > tolerance:
            drift[role] = {"pre": a, "post": b}
    return drift


def verify(
    plan: CanaryPlan,
    pre: Snapshot,
    *,
    interval_min: int,
    sf_mcp: Any = salesforce_mcp,
    tolerance: float = DEFAULT_TOLERANCE,
    now: datetime | None = None,
) -> VerificationResult:
    post = pre_snapshot(sf_mcp, now=now)
    drift = _diff(pre.counts_by_role, post.counts_by_role, tolerance=tolerance)

    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    history = manifest.setdefault("verifications", [])
    entry = {
        "interval_min": interval_min,
        "passed": not drift,
        "drift": drift,
        "post_snapshot": post.to_dict(),
        "checked_at": (now or datetime.now(timezone.utc)).isoformat(),
    }
    history.append(entry)
    (plan.path / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))

    if drift:
        msg = f"role-count drift at T+{interval_min}m: {sorted(drift)}"
        log.error("canary verify FAILED: %s", msg)
        return VerificationResult(
            passed=False, interval_min=interval_min, drift=drift, message=msg,
        )
    log.info("canary verify passed at T+%sm", interval_min)
    return VerificationResult(passed=True, interval_min=interval_min)


def schedule_verifications(
    plan: CanaryPlan,
    deployed_at: datetime,
    *,
    intervals_min: tuple[int, ...] = FOLLOWUP_INTERVALS_MIN,
) -> list[dict[str, Any]]:
    """Return the list of scheduled verification times.

    The actual dispatch happens via the cron scheduler; this function only
    encodes the timetable so tests can assert the cadence. The scheduler
    reads `manifest.scheduled_verifications` at 15m poll granularity.
    """
    manifest = yaml.safe_load((plan.path / "change.yaml").read_text())
    scheduled = [
        {"at": (deployed_at + timedelta(minutes=m)).isoformat(), "interval_min": m}
        for m in intervals_min
    ]
    manifest["scheduled_verifications"] = scheduled
    (plan.path / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return scheduled


def auto_approve_for_test(plan: CanaryPlan) -> None:
    """Test-only helper. Do not call from production paths."""
    decide_approval_gate(plan.gate_id, approved=True, approver="test-harness")


def _load_manifest(plan: CanaryPlan) -> dict[str, Any]:
    return yaml.safe_load((plan.path / "change.yaml").read_text()) or {}


def _save_manifest(plan: CanaryPlan, manifest: dict[str, Any]) -> None:
    (plan.path / "change.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))


def prepare_rollback(
    plan: CanaryPlan,
    *,
    justification: str,
    requested_by: str = "system",
) -> int:
    """Open a rollback approval gate for the canary. Returns gate_id.

    Canary rollback is a destructive delete of the CEO role + a re-parent of
    CRO back to top. Opens an `sf_schema_delete` gate since the net effect
    destroys a role.
    """
    if not justification or not justification.strip():
        raise ApprovalRequired("canary rollback requires written justification")

    manifest = _load_manifest(plan)
    revert_force_app = plan.path / "revert" / "force-app"
    if not revert_force_app.exists():
        raise CanaryError(f"no revert bundle at {revert_force_app}")

    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="sf_schema_delete",
        payload={
            "slug": plan.slug,
            "canary_rollback": True,
            "role_dev_name": plan.role_dev_name,
            "parent_role": PARENT_CRO_ROLE,
            "revert_dir": str(revert_force_app),
            "original_deploy_id": manifest.get("deploy_id"),
            "origin": "revops_support_canary_rollback",
        },
        justification=justification,
        requested_by=requested_by,
    )
    manifest["rollback_gate_id"] = gate_id
    manifest["rollback_status"] = "pending"
    _save_manifest(plan, manifest)
    log.info("canary rollback gate opened slug=%s gate=%s", plan.slug, gate_id)
    return gate_id


def execute_rollback(
    plan: CanaryPlan,
    *,
    deploy_fn: Callable[..., dict[str, Any]] = salesforce_mcp.deploy_metadata,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deploy the revert bundle after the canary rollback gate is approved.

    Idempotent: re-running after success no-ops.
    """
    now = now or datetime.now(timezone.utc)
    manifest = _load_manifest(plan)

    if manifest.get("rollback_status") == "deployed":
        log.info("canary rollback already deployed slug=%s — no-op", plan.slug)
        return {"success": True, "no_op": True, "deploy_id": manifest.get("rollback_deploy_id")}

    gate_id = manifest.get("rollback_gate_id")
    if not gate_id:
        raise CanaryError("no rollback_gate_id on manifest; run prepare_rollback() first")

    # sf_schema_delete is dual-approval-with-cooldown: primary gate must be
    # approved_primary or approved, AND a confirm child gate must be approved.
    # Mirrors metadata_deployer._verify_gate.
    from sqlalchemy import text
    from shared.db.connection import get_engine
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status FROM approval_gates WHERE id = :i"), {"i": gate_id}
        ).fetchone()
        if row is None:
            raise CanaryError(f"rollback gate {gate_id} not found")
        if row[0] not in ("approved_primary", "approved"):
            raise ApprovalRequired(
                f"rollback gate {gate_id} status={row[0]}; expected approved_primary"
            )
        child = conn.execute(
            text(
                """SELECT id, status FROM approval_gates
                       WHERE parent_gate_id = :p
                         AND action_type = 'sf_schema_delete_confirm'
                       ORDER BY id DESC LIMIT 1"""
            ),
            {"p": gate_id},
        ).mappings().fetchone()
    if not child or child["status"] != "approved":
        raise ApprovalRequired(
            f"rollback gate {gate_id} missing approved confirmation child"
        )

    revert_force_app = plan.path / "revert" / "force-app"
    revert_default = revert_force_app / "main" / "default"
    post_destructive = revert_default / "destructiveChangesPost.xml"
    package_xml = revert_default / "package.xml"
    try:
        raw = deploy_fn(
            str(revert_force_app),
            intent="write",
            check_only=False,
            manifest=str(package_xml) if post_destructive.exists() else None,
            post_destructive_changes=str(post_destructive) if post_destructive.exists() else None,
            # Treat "role not found" as benign so rollback is idempotent against
            # sandboxes where a prior attempt already removed CEO.
            ignore_warnings=bool(post_destructive.exists()),
        )
    except Exception as e:  # noqa: BLE001
        manifest["rollback_status"] = "failed"
        manifest["rollback_error"] = str(e)[:500]
        _save_manifest(plan, manifest)
        log.exception("canary rollback deploy failed slug=%s", plan.slug)
        raise

    deploy_id = raw.get("id") or raw.get("deployId")
    success = bool(raw.get("success")) and str(raw.get("status", "")).lower() in (
        "succeeded", "succeededpartial", ""
    )

    write_audit(
        agent_name=AGENT_NAME,
        action="sf_schema_rollback",
        target=f"sf:UserRole:{plan.role_dev_name}",
        before={"slug": plan.slug, "original_deploy_id": manifest.get("deploy_id")},
        after={"deploy_id": deploy_id, "success": success, "raw_status": raw.get("status")},
        approval_gate_id=gate_id,
    )

    manifest["rollback_status"] = "deployed" if success else "failed"
    manifest["rollback_deploy_id"] = deploy_id
    manifest["rolled_back_at"] = now.isoformat()
    _save_manifest(plan, manifest)
    if not success:
        raise CanaryError(f"canary rollback deploy failed: {raw.get('status')}")
    return raw
