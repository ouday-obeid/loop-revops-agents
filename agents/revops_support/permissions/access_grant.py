"""Grant or revoke PermissionSets and Public Group memberships.

All writes flow through salesforce_mcp.create_record / update_record so the
approval gate (`permission_grant` action_type) is enforced uniformly.

Revoking a PermissionSetAssignment or GroupMember requires a DELETE via the
SF CLI's `sf data delete-record` — we wrap that here rather than extending
salesforce_mcp because delete is only valid for very small record classes
(not a general-purpose DELETE primitive we want elsewhere).
"""
from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any

from shared.governance import require_approved_gate, write_audit
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)


@dataclass
class GrantResult:
    assignment_id: str
    was_existing: bool


def _find_existing_assignment(
    user_id: str, permission_set_id: str, *, sf_mcp: Any = salesforce_mcp,
) -> str | None:
    q = (
        "SELECT Id FROM PermissionSetAssignment "
        f"WHERE AssigneeId = '{user_id}' AND PermissionSetId = '{permission_set_id}'"
    )
    r = sf_mcp.soql_query(q, limit=1)
    records = r.get("records") or []
    return records[0].get("Id") if records else None


def grant_permission_set(
    user_id: str,
    permission_set_id: str,
    *,
    approval_gate_id: int,
    agent_name: str = "revops_support",
    sf_mcp: Any = salesforce_mcp,
) -> GrantResult:
    """Idempotent: return existing assignment if one already covers this user × PS."""
    existing = _find_existing_assignment(user_id, permission_set_id, sf_mcp=sf_mcp)
    if existing:
        return GrantResult(assignment_id=existing, was_existing=True)
    created = sf_mcp.create_record(
        "PermissionSetAssignment",
        {"AssigneeId": user_id, "PermissionSetId": permission_set_id},
        agent_name=agent_name, approval_gate_id=approval_gate_id,
    )
    return GrantResult(
        assignment_id=created.get("id") or created.get("Id") or "",
        was_existing=False,
    )


def revoke_permission_set(
    user_id: str,
    permission_set_id: str,
    *,
    approval_gate_id: int,
    agent_name: str = "revops_support",
    sf_mcp: Any = salesforce_mcp,
    sf_delete: Any = None,
) -> str | None:
    """Delete the PermissionSetAssignment row if present. Returns deleted id or None."""
    require_approved_gate(approval_gate_id, action_type="permission_grant")
    existing = _find_existing_assignment(user_id, permission_set_id, sf_mcp=sf_mcp)
    if not existing:
        return None
    _delete_record("PermissionSetAssignment", existing, sf_delete=sf_delete)
    write_audit(
        agent_name=agent_name,
        action="sf_delete",
        target=f"sf:PermissionSetAssignment:{existing}",
        before={"AssigneeId": user_id, "PermissionSetId": permission_set_id},
        approval_gate_id=approval_gate_id,
    )
    return existing


def add_to_group(
    user_id: str,
    group_id: str,
    *,
    approval_gate_id: int,
    agent_name: str = "revops_support",
    sf_mcp: Any = salesforce_mcp,
) -> GrantResult:
    q = (
        "SELECT Id FROM GroupMember "
        f"WHERE UserOrGroupId = '{user_id}' AND GroupId = '{group_id}'"
    )
    r = sf_mcp.soql_query(q, limit=1)
    records = r.get("records") or []
    if records:
        return GrantResult(assignment_id=records[0].get("Id"), was_existing=True)
    created = sf_mcp.create_record(
        "GroupMember", {"GroupId": group_id, "UserOrGroupId": user_id},
        agent_name=agent_name, approval_gate_id=approval_gate_id,
    )
    return GrantResult(
        assignment_id=created.get("id") or created.get("Id") or "",
        was_existing=False,
    )


def _delete_record(sobject: str, record_id: str, *, sf_delete: Any = None) -> None:
    """Wrap `sf data delete-record` — SF CLI doesn't ship a Python client."""
    if sf_delete is not None:
        sf_delete(sobject, record_id)
        return
    alias = salesforce_mcp._resolve_org_alias("write")
    cmd = [
        "sf", "data", "delete-record",
        "--sobject", sobject,
        "--record-id", record_id,
        "--json",
    ]
    if alias:
        cmd.extend(["--target-org", alias])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise salesforce_mcp.SalesforceError(
            f"sf delete-record failed: {(proc.stderr or proc.stdout)[:200]}"
        ) from e
    if data.get("status") not in (0, None):
        raise salesforce_mcp.SalesforceError(data.get("message") or "delete failed")
