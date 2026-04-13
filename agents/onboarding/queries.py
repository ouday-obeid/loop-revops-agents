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

# Active onboardings to evaluate for stalls. Account access is through the
# Opportunity lookup — Onboarding__c has no direct Account__c field.
ACTIVE_ONBOARDINGS = (
    "SELECT Id, Name, Opportunity__r.AccountId, Opportunity__r.Account.Name, "
    "OwnerId, CSM_2__c, Opportunity__c, "
    "JK_Onboarding_Stage__c, Overall_Onboarding_Status__c, Kickoff_Status__c, "
    "Onboarding_Health__c, LastModifiedDate, "
    "DS_Kickoff_Status_Scheduled__c, DS_Kickoff_Status_Held__c, "
    "DS_Overall_Onboarding_Status_In_Progress__c, "
    "DS_Overall_Onboarding_Status_Completed__c "
    "FROM Onboarding__c "
    "WHERE Overall_Onboarding_Status__c IN ('Not Started', 'In Progress')"
)


# ---------- Location activation ----------

# Location__c in the Loop org has no Account__c / Activation_Status__c /
# Stuck_Reason__c fields — the brief assumed them, the real schema has only
# TLO__c (Top_Level_Organization__c) and Active__c (boolean). The location
# activation feature is schema-gapped until the fields are added (Agent 5
# task auto-seeded from location_activation.py at boot).
#
# Until then, LOCATIONS_BY_TLO + ACTIVE_LOCATIONS are the best we can do:
# Active__c = false signals "not activated"; Stuck_Reason__c is unavailable.
LOCATIONS_BY_TLO = (
    "SELECT Id, Name, TLO__c, Active__c, LastModifiedDate "
    "FROM Location__c WHERE TLO__c = '{tlo_id}'"
)

INACTIVE_LOCATIONS_ALL = (
    "SELECT Id, Name, TLO__c, TLO__r.Name, Active__c, LastModifiedDate "
    "FROM Location__c WHERE Active__c = false"
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
