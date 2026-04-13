"""Create a Salesforce User, assign profile/role, attach perm sets + groups.

One-stop entrypoint: `provision(profile_payload, approval_gate_id)`. The
caller is responsible for having obtained an *approved* gate of action_type
`user_provisioning` before calling; the underlying create_record calls will
still re-check the gate per write.

We explicitly do NOT accept a plaintext password — Salesforce auto-generates
one and emails it when `SendEmailOnCreation` is true on the user's profile.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

# Fields SF requires on User create.
REQUIRED_USER_FIELDS: tuple[str, ...] = (
    "FirstName", "LastName", "Email", "Username", "Alias",
    "ProfileId", "TimeZoneSidKey", "LocaleSidKey", "EmailEncodingKey",
    "LanguageLocaleKey",
)

DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_LOCALE = "en_US"
DEFAULT_ENCODING = "UTF-8"
DEFAULT_LANGUAGE = "en_US"


@dataclass
class ProvisionRequest:
    first_name: str
    last_name: str
    email: str
    username: str
    alias: str
    profile_id: str
    role_id: str | None = None
    permission_set_ids: list[str] = field(default_factory=list)
    group_ids: list[str] = field(default_factory=list)
    extra_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProvisionResult:
    user_id: str
    permission_set_assignments: list[str] = field(default_factory=list)
    group_memberships: list[str] = field(default_factory=list)


def _user_field_payload(req: ProvisionRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "FirstName": req.first_name,
        "LastName": req.last_name,
        "Email": req.email,
        "Username": req.username,
        "Alias": req.alias,
        "ProfileId": req.profile_id,
        "TimeZoneSidKey": DEFAULT_TIMEZONE,
        "LocaleSidKey": DEFAULT_LOCALE,
        "EmailEncodingKey": DEFAULT_ENCODING,
        "LanguageLocaleKey": DEFAULT_LANGUAGE,
    }
    if req.role_id:
        payload["UserRoleId"] = req.role_id
    payload.update(req.extra_fields or {})
    return payload


def provision(
    req: ProvisionRequest,
    *,
    approval_gate_id: int,
    agent_name: str = "revops_support",
    sf_mcp: Any = salesforce_mcp,
) -> ProvisionResult:
    """Create User, then attach each PermissionSet and Group.

    Each sub-write uses the same approval_gate_id; the gate is scoped to the
    full provisioning action (not one gate per sub-write).
    """
    payload = _user_field_payload(req)
    missing = [f for f in REQUIRED_USER_FIELDS if not payload.get(f)]
    if missing:
        raise ValueError(f"user provisioning missing required fields: {missing}")

    created = sf_mcp.create_record(
        "User", payload,
        agent_name=agent_name, approval_gate_id=approval_gate_id,
    )
    user_id = created.get("id") or created.get("Id")
    if not user_id:
        raise RuntimeError(f"sf create User did not return an id: {created!r}")

    result = ProvisionResult(user_id=user_id)

    for ps_id in req.permission_set_ids:
        rec = sf_mcp.create_record(
            "PermissionSetAssignment",
            {"AssigneeId": user_id, "PermissionSetId": ps_id},
            agent_name=agent_name, approval_gate_id=approval_gate_id,
        )
        result.permission_set_assignments.append(rec.get("id") or rec.get("Id") or "")

    for g_id in req.group_ids:
        rec = sf_mcp.create_record(
            "GroupMember",
            {"GroupId": g_id, "UserOrGroupId": user_id},
            agent_name=agent_name, approval_gate_id=approval_gate_id,
        )
        result.group_memberships.append(rec.get("id") or rec.get("Id") or "")

    log.info(
        "provisioned user id=%s perm_sets=%d groups=%d",
        user_id, len(result.permission_set_assignments), len(result.group_memberships),
    )
    return result
