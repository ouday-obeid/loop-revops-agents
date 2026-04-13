"""Slack action handlers for CS-owned approval gates.

The shared dispatcher in `shared/slack_dispatcher.py` handles the generic
approve/reject button click and flips `approval_gates.status`. The functions
here run AFTER that transition — they own the CS-specific side effects:

  1. CSM reassignment (`csm_reassignment`)
        approve → update_record('Account', id, {OwnerId: new_owner})

  2. Churn-prevention outreach (`cs_churn_outreach`)
        approve → post approved draft to the CSM Slack DM (no customer write).

  3. Mark Churned — dual approval
        gate A (`mark_churned_request`, Jackie) →
        on approve, auto-create gate B (`mark_churned_confirm`, O) with
        `parent_gate_id = A.id` and the same payload →
        on approve of B, verify A is still approved + matches parent →
        update_record('Account', id, {Churn_Status__c: 'Churned'}).

Public surface (all synchronous — called from the button handler):

    request_csm_reassignment(...)
    finalize_csm_reassignment(gate_id, new_owner_id, approver)

    request_churn_outreach(...)
    finalize_churn_outreach(gate_id, approver, sf_mcp=None, slack_sender=None)

    request_mark_churned(...)              # opens Gate A
    on_mark_churned_primary_approved(a_id) # called when A flips approved
    finalize_mark_churned(b_id, approver, sf_mcp=None)

Any gate expiration or mismatch raises ApprovalRequired — the generic
dispatcher path already guards against non-approved gates.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import (
    ApprovalRequired,
    create_approval_gate,
    get_approval_gate,
    require_approved_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp as _sf_default
from shared.secrets import get_config

log = logging.getLogger(__name__)

AGENT_NAME = "cs"


def _jackie_channel() -> str:
    return get_config("CS_JACKIE_CHANNEL", "#agent-cs-log")


def _payload(gate: dict[str, Any]) -> dict[str, Any]:
    raw = gate.get("payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


# ---------------------------------------------------------------------------
# 1. CSM reassignment
# ---------------------------------------------------------------------------

def request_csm_reassignment(
    *,
    account_id: str,
    old_owner_id: str | None,
    new_owner_id: str,
    reason: str,
    slack_sender: Any | None = None,
) -> int:
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="csm_reassignment",
        payload={
            "account_id": account_id,
            "old_owner_id": old_owner_id,
            "new_owner_id": new_owner_id,
            "reason": reason,
        },
        justification=reason,
        requested_by=f"system:{AGENT_NAME}",
    )
    if slack_sender is not None:
        from shared.slack_dispatcher import approval_blocks
        summary = (
            f"*CSM reassignment* for account `{account_id}`: "
            f"`{old_owner_id or 'none'}` → `{new_owner_id}`\n_{reason}_"
        )
        slack_sender.send(_jackie_channel(), summary,
                          blocks=approval_blocks(gate_id, "csm_reassignment", summary))
    return gate_id


def finalize_csm_reassignment(
    gate_id: int,
    approver: str,
    *,
    sf_mcp: Any | None = None,
) -> dict[str, Any]:
    gate = require_approved_gate(gate_id, action_type="csm_reassignment")
    payload = _payload(gate)
    account_id = payload["account_id"]
    new_owner = payload["new_owner_id"]
    sf = sf_mcp or _sf_default
    result = sf.update_record(
        "Account", account_id, {"OwnerId": new_owner},
        agent_name=AGENT_NAME, approval_gate_id=gate_id,
    )
    write_audit(
        agent_name=AGENT_NAME,
        action="csm_reassigned",
        target=f"sf:Account:{account_id}",
        before={"owner_id": payload.get("old_owner_id")},
        after={"owner_id": new_owner, "approved_by": approver},
        approval_gate_id=gate_id,
    )
    return result


# ---------------------------------------------------------------------------
# 2. Churn outreach — draft review (no customer write)
# ---------------------------------------------------------------------------

def request_churn_outreach(
    *,
    account_id: str,
    csm_slack_id: str,
    draft_markdown: str,
    reason: str,
    slack_sender: Any | None = None,
) -> int:
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="cs_churn_outreach",
        payload={
            "account_id": account_id,
            "csm_slack_id": csm_slack_id,
            "draft": draft_markdown,
            "reason": reason,
        },
        justification=reason,
        requested_by=f"system:{AGENT_NAME}",
    )
    if slack_sender is not None:
        from shared.slack_dispatcher import approval_blocks
        summary = (
            f"*Churn outreach draft* for `{account_id}` (CSM <@{csm_slack_id}>)\n"
            f"_{reason}_\n\n{draft_markdown}"
        )
        slack_sender.send(_jackie_channel(), summary,
                          blocks=approval_blocks(gate_id, "cs_churn_outreach", summary))
    return gate_id


def finalize_churn_outreach(
    gate_id: int,
    approver: str,
    *,
    slack_sender: Any | None = None,
) -> dict[str, Any]:
    gate = require_approved_gate(gate_id, action_type="cs_churn_outreach")
    payload = _payload(gate)
    csm = payload["csm_slack_id"]
    draft = payload["draft"]
    posted = None
    if slack_sender is not None:
        posted = slack_sender.send(
            csm,
            f"Approved churn-outreach draft for `{payload['account_id']}` "
            f"(approved by {approver}):\n\n{draft}",
        )
    write_audit(
        agent_name=AGENT_NAME,
        action="churn_outreach_posted",
        target=f"slack:{csm}",
        before=None,
        after={"account_id": payload["account_id"], "approved_by": approver},
        approval_gate_id=gate_id,
    )
    return {"posted_to": csm, "slack": posted}


# ---------------------------------------------------------------------------
# 3. Mark Churned — dual approval (Jackie → O → SF write)
# ---------------------------------------------------------------------------

def request_mark_churned(
    *,
    account_id: str,
    justification: str,
    slack_sender: Any | None = None,
) -> int:
    """Open Gate A (Jackie) for a Mark Churned request."""
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="mark_churned_request",
        payload={"account_id": account_id, "justification": justification},
        justification=justification,
        requested_by=f"system:{AGENT_NAME}",
    )
    if slack_sender is not None:
        from shared.slack_dispatcher import approval_blocks
        summary = (
            f"*Mark Churned* requested for `{account_id}`\n_{justification}_\n"
            "Jackie approves first, then O confirms."
        )
        slack_sender.send(_jackie_channel(), summary,
                          blocks=approval_blocks(gate_id, "mark_churned_request", summary))
    return gate_id


def on_mark_churned_primary_approved(
    primary_gate_id: int,
    *,
    slack_sender: Any | None = None,
) -> int:
    """Called when Gate A flips to 'approved'. Creates Gate B linked via
    parent_gate_id; returns the new gate id.

    Idempotent: if a confirm gate already exists for this parent, returns it.
    """
    gate_a = get_approval_gate(primary_gate_id)
    if not gate_a or gate_a["action_type"] != "mark_churned_request":
        raise ApprovalRequired(
            f"primary gate {primary_gate_id} is not a mark_churned_request"
        )
    if gate_a["status"] != "approved":
        raise ApprovalRequired(
            f"primary gate {primary_gate_id} status={gate_a['status']} "
            "(required: approved)"
        )

    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text(
                """SELECT id FROM approval_gates
                    WHERE parent_gate_id = :p
                      AND action_type = 'mark_churned_confirm'
                      AND status IN ('pending', 'approved')"""
            ),
            {"p": primary_gate_id},
        ).fetchone()
    if existing:
        return int(existing[0])

    payload_a = _payload(gate_a)
    justification = payload_a.get("justification") or gate_a.get("justification") or ""
    confirm_payload = {
        "account_id": payload_a["account_id"],
        "justification": justification,
        "parent_gate_id": primary_gate_id,
    }
    # Reuse create_approval_gate; patch parent_gate_id directly (not exposed
    # in the create API so we set it post-insert).
    b_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="mark_churned_confirm",
        payload=confirm_payload,
        justification=justification,
        requested_by=f"system:{AGENT_NAME}",
    )
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE approval_gates SET parent_gate_id = :p WHERE id = :id"),
            {"p": primary_gate_id, "id": b_id},
        )

    if slack_sender is not None:
        from shared.slack_dispatcher import approval_blocks
        summary = (
            f"*Mark Churned* — Jackie approved gate `{primary_gate_id}` for "
            f"`{payload_a['account_id']}`. O's confirmation needed.\n"
            f"_{justification}_"
        )
        slack_sender.send(_jackie_channel(), summary,
                          blocks=approval_blocks(b_id, "mark_churned_confirm", summary))
    return b_id


def finalize_mark_churned(
    confirm_gate_id: int,
    approver: str,
    *,
    sf_mcp: Any | None = None,
) -> dict[str, Any]:
    """Execute the SF write after Gate B is approved.

    Verifies that:
      - confirm gate is approved + action_type=mark_churned_confirm
      - parent gate A exists, is action_type=mark_churned_request, status=approved
      - confirm.payload.parent_gate_id matches gate A's id
      - account_ids agree between A and B
    """
    gate_b = require_approved_gate(confirm_gate_id, action_type="mark_churned_confirm")
    payload_b = _payload(gate_b)
    parent_id = payload_b.get("parent_gate_id") or gate_b.get("parent_gate_id")
    if not parent_id:
        raise ApprovalRequired(
            f"confirm gate {confirm_gate_id} missing parent_gate_id reference"
        )

    gate_a = get_approval_gate(int(parent_id))
    if not gate_a:
        raise ApprovalRequired(f"primary gate {parent_id} not found")
    if gate_a["action_type"] != "mark_churned_request":
        raise ApprovalRequired(
            f"parent gate {parent_id} action_type={gate_a['action_type']} "
            "(expected mark_churned_request)"
        )
    if gate_a["status"] != "approved":
        raise ApprovalRequired(
            f"parent gate {parent_id} status={gate_a['status']} (required: approved)"
        )

    payload_a = _payload(gate_a)
    if payload_a.get("account_id") != payload_b.get("account_id"):
        raise ApprovalRequired(
            f"account_id mismatch: A={payload_a.get('account_id')} "
            f"B={payload_b.get('account_id')}"
        )

    account_id = payload_b["account_id"]
    sf = sf_mcp or _sf_default
    result = sf.update_record(
        "Account", account_id, {"Churn_Status__c": "Churned"},
        agent_name=AGENT_NAME, approval_gate_id=confirm_gate_id,
    )
    write_audit(
        agent_name=AGENT_NAME,
        action="marked_churned",
        target=f"sf:Account:{account_id}",
        before=None,
        after={
            "primary_gate_id": int(parent_id),
            "confirm_gate_id": confirm_gate_id,
            "approved_by": approver,
            "justification": payload_b.get("justification"),
        },
        approval_gate_id=confirm_gate_id,
    )
    return result


# Async shim so shared/slack_dispatcher.register("cs", handle_action) still works
# if ever wired directly. Real routing happens via the module-level functions
# above, called from the post-decision hook.
async def handle_action(payload: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "note": "cs slack_actions — call module functions directly"}


__all__ = [
    "request_csm_reassignment",
    "finalize_csm_reassignment",
    "request_churn_outreach",
    "finalize_churn_outreach",
    "request_mark_churned",
    "on_mark_churned_primary_approved",
    "finalize_mark_churned",
    "handle_action",
]
