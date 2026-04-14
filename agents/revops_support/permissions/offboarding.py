"""Offboard a Salesforce User.

Sequence (each gated by a `license_deactivation` approval):
  1. Build an ownership manifest — count of records a user owns across the
     sobjects we care about. If `transfer_to_user_id` is set, build bulk
     update payloads for each sobject to reassign OwnerId.
  2. Execute those bulk updates via `agents.revops_support.data_quality.bulk_updater`
     (reuses the composite-API + audit-snapshot machinery).
  3. Update User.IsActive = false.

Package-access revocation (ConnectedApp / InstalledPackage per-user settings)
is NOT automated — SF does not expose a clean SOQL/CLI path. Instead, we open
a `package_access_revoke_pending` task at the end so O can clear the flag
manually in setup.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from shared.mcp import salesforce_mcp
from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)

# Objects whose Owner we know how to safely reassign on offboarding.
REASSIGN_SOBJECTS: tuple[str, ...] = (
    "Account", "Opportunity", "Lead", "Case",
)


@dataclass
class OffboardRequest:
    user_id: str
    transfer_to_user_id: str | None
    reassign_sobjects: tuple[str, ...] = REASSIGN_SOBJECTS


@dataclass
class OffboardResult:
    user_id: str
    deactivated: bool
    reassigned: dict[str, int] = field(default_factory=dict)  # sobject → count
    manual_followups: list[str] = field(default_factory=list)


def _count_owned(sobject: str, user_id: str, *, sf_mcp: Any = salesforce_mcp) -> int:
    q = f"SELECT COUNT(Id) c FROM {sobject} WHERE OwnerId = '{user_id}'"
    r = sf_mcp.soql_query(q, limit=1)
    # SF COUNT() may come back as `totalSize` or `records[0].c`; handle both.
    records = r.get("records") or []
    if records and "expr0" in records[0]:
        return int(records[0]["expr0"])
    if records and "c" in records[0]:
        return int(records[0]["c"])
    return int(r.get("totalSize") or 0)


def _ids_owned(sobject: str, user_id: str, *, sf_mcp: Any = salesforce_mcp) -> list[str]:
    q = f"SELECT Id FROM {sobject} WHERE OwnerId = '{user_id}'"
    r = sf_mcp.soql_query(q, limit=50000)
    return [rec["Id"] for rec in r.get("records") or [] if rec.get("Id")]


def offboard(
    req: OffboardRequest,
    *,
    approval_gate_id: int,
    agent_name: str = "revops_support",
    sf_mcp: Any = salesforce_mcp,
    bulk_updater: Any | None = None,
) -> OffboardResult:
    result = OffboardResult(user_id=req.user_id, deactivated=False)

    if req.transfer_to_user_id:
        if bulk_updater is None:
            from agents.revops_support.data_quality import bulk_updater as _bulk
            bulk_updater = _bulk
        for s in req.reassign_sobjects:
            ids = _ids_owned(s, req.user_id, sf_mcp=sf_mcp)
            if not ids:
                continue
            updates = [
                {"Id": rid, "OwnerId": req.transfer_to_user_id} for rid in ids
            ]
            bulk_updater.bulk_update(
                s, updates,
                agent_name=agent_name,
                approval_gate_id=approval_gate_id,
            )
            result.reassigned[s] = len(ids)
    else:
        # No transfer target: at least report the blast radius so O knows
        # what's about to orphan.
        for s in req.reassign_sobjects:
            result.reassigned[s] = _count_owned(s, req.user_id, sf_mcp=sf_mcp)

    # Deactivate.
    sf_mcp.update_record(
        "User", req.user_id, {"IsActive": False},
        agent_name=agent_name, approval_gate_id=approval_gate_id,
    )
    result.deactivated = True

    _open_manual_followup(req.user_id)
    result.manual_followups.append("package_access_revoke_pending")
    return result


def _open_manual_followup(user_id: str) -> None:
    source = f"revops_support:offboarding:package_access:{user_id}"
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s AND status != 'completed' LIMIT 1"),
            {"s": source},
        ).fetchone()
        if exists:
            return
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('revops_support', :t, :d, 'pending', 'medium',
                           'sf_offboarding', :s, 'O', :m)"""
            ),
            {
                "t": f"Revoke package access for {user_id}",
                "d": (
                    "Offboarding completed deactivation + ownership reassign. "
                    "Remaining step: manually revoke ConnectedApp / installed "
                    "package access in Setup (no SF API surface for this)."
                ),
                "s": source,
                "m": json.dumps({"user_id": user_id}),
            },
        )
