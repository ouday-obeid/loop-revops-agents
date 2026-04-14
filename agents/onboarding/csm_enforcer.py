"""CSM enforcer — catches Onboarding__c records with a null OwnerId.

Two entry points:

  * `handle_created(opp, onboarding_id)` — called by the record creator the
    instant an Onboarding__c is created with no owner. Immediate DM to Jackie
    + O with a Reassign button.

  * `sweep()` — periodic check for records that LOST their owner between
    create and now (reassignment flows, user deactivation, etc).

Reassignment goes through an approval gate (`csm_reassignment` tier) so the
change is audited and reversible. The gate payload carries the old and new
OwnerIds; the handler resolves the gate on button click.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.db.connection import get_engine
from shared.governance import (
    create_approval_gate,
    require_approved_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp
from shared.secrets import get_config

log = logging.getLogger(__name__)

AGENT_NAME = "onboarding"


def _jackie_channel() -> str:
    return get_config("ONBOARDING_JACKIE_CHANNEL", "#agent-onboarding-log")


def handle_created(opp: dict[str, Any], onboarding_id: str) -> dict[str, Any] | None:
    """If the opp has no OwnerId, DM Jackie + O to assign a CSM."""
    if opp.get("OwnerId"):
        return None
    account = (opp.get("Account") or {}).get("Name") or opp.get("AccountId") or "(no account)"
    summary = (
        f"New *Onboarding__c* `{onboarding_id}` for *{account}* (opp `{opp['Id']}`) "
        f"was created with no CSM assigned. Who should own it?"
    )
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="csm_reassignment",
        payload={
            "onboarding_id": onboarding_id,
            "opportunity_id": opp["Id"],
            "account_id": opp.get("AccountId"),
            "current_owner_id": None,
            "proposed_owner_id": None,  # Jackie fills in via Slack thread
            "trigger": "unassigned_on_create",
        },
        justification=None,
        requested_by=f"system:{AGENT_NAME}",
    )
    from shared.slack_dispatcher import SlackSender, approval_blocks
    blocks = approval_blocks(gate_id, "csm_reassignment", summary)
    SlackSender().send(_jackie_channel(), summary, blocks=blocks)
    log.info("csm_reassignment gate=%s posted for onboarding=%s", gate_id, onboarding_id)
    return {"gate_id": gate_id, "posted": True}


async def sweep() -> dict[str, Any]:
    """Find onboardings whose OwnerId has gone null after creation."""
    query = (
        "SELECT Id, Name, Opportunity__r.Account.Name, OwnerId, CSM_2__c, "
        "Opportunity__c FROM Onboarding__c WHERE OwnerId = null "
        "AND Overall_Onboarding_Status__c NOT IN ('Completed', 'DOA', 'Failed to Go Live') "
        "LIMIT 100"
    )
    res = salesforce_mcp.soql_query(query)
    rows = res.get("records") or []
    from shared.slack_dispatcher import SlackSender, approval_blocks
    posted = 0
    for row in rows:
        gate_id = create_approval_gate(
            agent_name=AGENT_NAME,
            action_type="csm_reassignment",
            payload={
                "onboarding_id": row["Id"],
                "opportunity_id": row.get("Opportunity__c"),
                "trigger": "sweep_found_null_owner",
            },
            justification=None,
            requested_by=f"system:{AGENT_NAME}",
        )
        opp_rel = row.get("Opportunity__r") or {}
        account = ((opp_rel.get("Account") or {}).get("Name")) or "(unknown)"
        summary = (
            f"*Onboarding__c* `{row['Id']}` for *{account}* has no CSM. "
            f"(Current CSM 2: `{row.get('CSM_2__c') or 'none'}`)"
        )
        blocks = approval_blocks(gate_id, "csm_reassignment", summary)
        SlackSender().send(_jackie_channel(), summary, blocks=blocks)
        posted += 1
    return {"unassigned": len(rows), "posted": posted}


def apply_reassignment(
    gate_id: int,
    *,
    new_owner_id: str,
    approver: str,
) -> dict[str, Any]:
    """Executed when a CSM reassignment gate is approved and a new owner is set.

    Called by the dispatcher's handler for the Approve button / slash command
    that fills in the `proposed_owner_id`. Uses the SF MCP to write the new
    OwnerId, then audits.
    """
    # Approval gate was decided via handle_gate_decision in slack_dispatcher;
    # require_approved_gate asserts status=='approved' + action_type match.
    gate = require_approved_gate(gate_id, action_type="csm_reassignment")
    payload = _extract_payload(gate)
    onboarding_id = payload.get("onboarding_id")
    if not onboarding_id:
        raise ValueError(f"gate {gate_id} missing onboarding_id in payload")

    result = salesforce_mcp.update_record(
        "Onboarding__c",
        onboarding_id,
        {"OwnerId": new_owner_id},
        agent_name=AGENT_NAME,
        approval_gate_id=gate_id,
    )
    write_audit(
        agent_name=AGENT_NAME,
        action="csm_reassigned",
        target=f"sf:Onboarding__c:{onboarding_id}",
        before={"owner_id": None},
        after={"owner_id": new_owner_id, "approved_by": approver},
        approval_gate_id=gate_id,
    )
    log.info("csm reassigned onboarding=%s new_owner=%s gate=%s",
             onboarding_id, new_owner_id, gate_id)
    return result


def _extract_payload(gate: dict[str, Any]) -> dict[str, Any]:
    raw = gate.get("payload")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        import json
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}
