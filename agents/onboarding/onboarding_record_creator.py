"""Onboarding__c field mapping + create.

The agent writes only the fields that must be set on creation. Everything else
(ACV, AE, Contract_Length, Account_Type, etc.) is a formula sourced from
`Opportunity__c` — touching those would shadow the formula and is forbidden.

Initial state we set:
  Name                          = f"{Account.Name} Onboarding"
  Opportunity__c                = Opp.Id       (formulas key off this)
  Account__c                    = Opp.AccountId
  OwnerId                       = Opp.OwnerId  (null → csm_enforcer picks it up)
  Overall_Onboarding_Status__c  = 'Not Started'
  JK_Onboarding_Stage__c        = 'Getting Access'
  Kickoff_Status__c             = 'Not Scheduled'

Plus the required booleans (no SF defaults): Auto_Iterate__c,
Balance_Discovery_Meeting__c, Balance_Included__c, Embargoed_Onboarding__c,
Full_Recon__c, Headroom_Analysis__c, Needs_Oversight_Check__c — each set to
False. Jackie flips them during onboarding as decisions are made.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.mcp import salesforce_mcp
from shared.secrets import get_config

log = logging.getLogger(__name__)

AGENT_NAME = "onboarding"

REQUIRED_BOOLEANS: tuple[str, ...] = (
    "Auto_Iterate__c",
    "Balance_Discovery_Meeting__c",
    "Balance_Included__c",
    "Embargoed_Onboarding__c",
    "Full_Recon__c",
    "Headroom_Analysis__c",
    "Needs_Oversight_Check__c",
)

# Fields that must never be written — all are formulas on Onboarding__c.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "ACV__c",
        "AE__c",
        "Account_Type__c",
        "Active_Products__c",
        "Close_Date__c",
        "Contract_Length_months__c",
        "Locs__c",
        "Opportunity_Owner__c",
        "Age__c",
        "OPP_Type__c",
        "CSM_owner_and_CSM_2__c",
    }
)


def _account_name(opp: dict[str, Any]) -> str:
    account = opp.get("Account") or {}
    return account.get("Name") or opp.get("AccountId", "")


def build_fields(opp: dict[str, Any]) -> dict[str, Any]:
    """Build the field dict for create_record. Does NOT call SF."""
    account_name = _account_name(opp) or "Unnamed Account"
    fields: dict[str, Any] = {
        "Name": f"{account_name} Onboarding",
        "Opportunity__c": opp["Id"],
        "Account__c": opp.get("AccountId"),
        "Overall_Onboarding_Status__c": "Not Started",
        "JK_Onboarding_Stage__c": "Getting Access",
        "Kickoff_Status__c": "Not Scheduled",
    }
    # OwnerId: set only when known. Null triggers csm_enforcer escalation.
    if opp.get("OwnerId"):
        fields["OwnerId"] = opp["OwnerId"]
    for name in REQUIRED_BOOLEANS:
        fields[name] = False
    _assert_no_forbidden(fields)
    return fields


def _assert_no_forbidden(fields: dict[str, Any]) -> None:
    """Guard against accidentally writing a formula field."""
    overlap = set(fields) & FORBIDDEN_FIELDS
    if overlap:
        raise ValueError(
            f"Refusing to set formula fields on Onboarding__c: {sorted(overlap)}. "
            "These are sourced from the Opportunity__c lookup."
        )


def create_from_opp(opp: dict[str, Any], *, gate_id: int) -> dict[str, Any]:
    """Create the Onboarding__c record and return the SF response."""
    fields = build_fields(opp)
    log.info("creating Onboarding__c for opp=%s owner=%s",
             opp["Id"], fields.get("OwnerId") or "UNASSIGNED")
    result = salesforce_mcp.create_record(
        "Onboarding__c",
        fields,
        agent_name=AGENT_NAME,
        approval_gate_id=gate_id,
    )
    onboarding_id = result.get("id") if isinstance(result, dict) else None
    _notify_cs_team(opp, result, fields.get("OwnerId"))
    if not fields.get("OwnerId") and onboarding_id:
        try:
            from agents.onboarding import csm_enforcer
            csm_enforcer.handle_created(opp, onboarding_id)
        except Exception:
            # A Slack/gate failure here must not break the create path.
            log.warning("csm_enforcer.handle_created failed opp=%s", opp["Id"],
                        exc_info=True)
    return result


def _notify_cs_team(opp: dict[str, Any], result: dict[str, Any], owner_id: str | None) -> None:
    """Post a Slack line to #cs-team. Dev guard will route to test channel."""
    try:
        from shared.slack_dispatcher import SlackSender

        account = _account_name(opp)
        channel = get_config("ONBOARDING_CS_TEAM_CHANNEL", "#cs-team")
        msg = (
            f"New onboarding created: *{account}* — "
            f"CSM: `{owner_id or 'UNASSIGNED'}` (opp `{opp['Id']}`)"
        )
        SlackSender().send(channel, msg)
    except Exception:  # never let a Slack failure break creation
        log.warning("slack notify failed for opp=%s", opp["Id"], exc_info=True)
