"""Centralized SOQL templates for the onboarding agent.

Why templates live here: the same query text is reused by the poller, the
monitor, and the @oo onboarding commands. Keeping them in one module makes
schema drift a one-file fix — if `JK_Onboarding_Stage__c` is ever renamed,
there's exactly one place to change it.

All queries are plain strings with f-string / format() substitution at call
sites. None of them accept user-supplied values directly (the agent only
injects SF IDs and known enum values), so there is no SOQL injection surface.
"""
from __future__ import annotations

# ---------- Closed Won trigger ----------

# Strategy A — preferred when Opportunity.Onboarding_Record_Created__c exists.
# Keeps the SOQL set small: only opps the agent has not yet processed.
CLOSED_WON_STRATEGY_A = (
    "SELECT Id, AccountId, Account.Name, OwnerId, CloseDate, Amount, Name "
    "FROM Opportunity "
    "WHERE StageName = 'Closed Won' "
    "AND Onboarding_Record_Created__c = false "
    "AND LastModifiedDate >= {last_poll_iso}"
)

# Strategy B — fallback when the dedup field hasn't been added yet.
# Pulls all recently-modified Closed Won opps; the poller then excludes any
# with an existing Onboarding__c. Slightly more SOQL but safe.
CLOSED_WON_STRATEGY_B = (
    "SELECT Id, AccountId, Account.Name, OwnerId, CloseDate, Amount, Name "
    "FROM Opportunity "
    "WHERE StageName = 'Closed Won' "
    "AND LastModifiedDate >= {last_poll_iso}"
)

# Used by Strategy B and by the belt-and-suspenders pre-create check.
EXISTING_ONBOARDINGS_FOR_OPPS = (
    "SELECT Id, Opportunity__c FROM Onboarding__c WHERE Opportunity__c IN ({opp_ids_quoted})"
)


# ---------- Schema pre-flight ----------

# Check whether the dedup field exists. Cheap; run once at boot.
ONBOARDING_CREATED_FIELD_EXISTS = (
    "SELECT QualifiedApiName FROM FieldDefinition "
    "WHERE EntityDefinition.QualifiedApiName = 'Opportunity' "
    "AND QualifiedApiName = 'Onboarding_Record_Created__c'"
)

# DocuSign status field migrated to PandaDoc in some envs — re-check at boot.
DOCUSIGN_STATUS_FIELD_EXISTS = (
    "SELECT QualifiedApiName FROM FieldDefinition "
    "WHERE EntityDefinition.QualifiedApiName = 'Opportunity' "
    "AND QualifiedApiName = 'DocuSign_Status__c'"
)


# ---------- Milestone monitor ----------

# Active onboardings to evaluate for stalls. Pulls DS_* timestamps for the
# "when did each stage last advance" inference — we stall when BOTH
# JK_Onboarding_Stage__c and Overall_Onboarding_Status__c haven't moved in
# ≥5 business days.
ACTIVE_ONBOARDINGS = (
    "SELECT Id, Name, Account__c, OwnerId, CSM_2__c, Opportunity__c, "
    "JK_Onboarding_Stage__c, Overall_Onboarding_Status__c, Kickoff_Status__c, "
    "Onboarding_Health__c, LastModifiedDate, "
    "DS_Kickoff_Status_Scheduled__c, DS_Kickoff_Status_Held__c, "
    "DS_Overall_Onboarding_Status_In_Progress__c, "
    "DS_Overall_Onboarding_Status_Completed__c "
    "FROM Onboarding__c "
    "WHERE Overall_Onboarding_Status__c IN ('Not Started', 'In Progress')"
)


# ---------- Location activation ----------

# Stuck locations — per plan, field name (Stuck_Reason__c / Activation_Status__c)
# is re-verified at boot via describe_sobject('Location__c'). If names differ,
# the agent logs loudly and flags a task for Agent 5 (RevOps Support).
LOCATIONS_BY_ACCOUNT = (
    "SELECT Id, Name, Account__c, Activation_Status__c, Stuck_Reason__c, "
    "LastModifiedDate "
    "FROM Location__c WHERE Account__c = '{account_id}'"
)

STUCK_LOCATIONS_ALL = (
    "SELECT Id, Name, Account__c, Account__r.Name, Activation_Status__c, "
    "Stuck_Reason__c, LastModifiedDate "
    "FROM Location__c WHERE Activation_Status__c != 'Active'"
)


# ---------- Handoff checklist ----------

OPP_LINE_ITEMS = (
    "SELECT Id, Product2Id, Product2.Name, UnitPrice, Quantity "
    "FROM OpportunityLineItem WHERE OpportunityId = '{opp_id}'"
)

OPP_CONTACT_ROLES = (
    "SELECT Id, ContactId, Contact.Name, Role, IsPrimary "
    "FROM OpportunityContactRole WHERE OpportunityId = '{opp_id}'"
)

OPP_DOCUSIGN_STATUS = (
    "SELECT Id, DocuSign_Status__c FROM Opportunity WHERE Id = '{opp_id}'"
)

ONBOARDING_KICKOFF_STATUS = (
    "SELECT Id, Kickoff_Status__c FROM Onboarding__c WHERE Opportunity__c = '{opp_id}'"
)

IMPLEMENTATION_PLAN_ATTACHED = (
    "SELECT Id, ContentDocumentId, ContentDocument.Title "
    "FROM ContentDocumentLink "
    "WHERE LinkedEntityId IN ({entity_ids_quoted}) "
    "AND ContentDocument.Title LIKE '%Implementation%'"
)


# ---------- Backfill ----------

HISTORICAL_CLOSED_WON_WITHOUT_ONBOARDING = (
    "SELECT COUNT() FROM Opportunity "
    "WHERE StageName = 'Closed Won' "
    "AND Id NOT IN (SELECT Opportunity__c FROM Onboarding__c WHERE Opportunity__c != null)"
)
