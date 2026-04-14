"""Salesforce fetchers — open pipeline + closed cohorts.

One SOQL per function, explicit LIMIT (shared.mcp.salesforce_mcp auto-injects
LIMIT 100 when absent — unsafe for a pipeline fetcher), and a narrow
`INVALID_FIELD: ICP_Score__c` retry path so the agent still boots in orgs that
haven't installed the ICP model yet (proxy path fills the gap).

Parsing is defensive: SF returns `null` for missing references, `{"attributes":
…}` wrappers around every row, and nested `OpportunityContactRoles.records`.
All coercion lives here so downstream scorers read clean OppRecord instances.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from shared.mcp import salesforce_mcp

from agents.slt_metrics.pipeline.config import (
    DEFAULT_FETCH_FROM,
    DEFAULT_FETCH_LIMIT,
    DEFAULT_FETCH_TO,
    PRODUCT_FIELDS,
)
from agents.slt_metrics.types import ContactRole, OppRecord

log = logging.getLogger(__name__)


# Column lists kept as tuples so the test can diff them against an expected set
# without string-parsing SOQL. Order matches LUCID's `forecast_scorer` fetch so
# the eventual Postgres-backed replay script has a one-to-one column map.
_CORE_COLUMNS: tuple[str, ...] = (
    "Id", "Name",
    "AccountId", "Account.Name", "Account.Website", "Account.Type",
    "OwnerId", "Owner.Name", "Owner.UserRole.Name", "Owner.Manager.Name",
    "StageName", "IsClosed", "IsWon",
    "Amount", "ACV__c", "Fixed_ARR__c",
    "Locations__c", "Type", "LeadSource",
    "CloseDate", "CreatedDate", "LastActivityDate", "LastModifiedDate",
    "LastStageChangeDate", "D_T_Last_Stage_Change__c", "Time_in_Stage__c",
    "Probability",
    "Description", "Next_Steps__c", "Next_Step_Date__c",
    "Segment__c",
)

_ICP_COLUMN: str = "ICP_Score__c"

_CONTACT_ROLES_SUBQUERY: str = (
    "(SELECT ContactId, Contact.Name, Contact.Email, Contact.Title, "
    "Role, IsPrimary FROM OpportunityContactRoles)"
)


def build_open_pipeline_soql(
    *,
    date_from: str = DEFAULT_FETCH_FROM,
    date_to: str = DEFAULT_FETCH_TO,
    limit: int = DEFAULT_FETCH_LIMIT,
    include_icp: bool = True,
) -> str:
    """Return the open-pipeline SOQL. Explicit LIMIT so `soql_query` does not
    auto-clamp to 100.

    `date_from` / `date_to` accept SOQL date literals (THIS_QUARTER, LAST_N_DAYS:30)
    or ISO dates — callers format appropriately.
    """
    columns = list(_CORE_COLUMNS)
    if include_icp:
        columns.append(_ICP_COLUMN)
    columns.extend(PRODUCT_FIELDS.keys())

    select_clause = ", ".join(columns) + ", " + _CONTACT_ROLES_SUBQUERY
    return (
        f"SELECT {select_clause} "
        "FROM Opportunity "
        f"WHERE IsClosed = false "
        f"AND CloseDate >= {date_from} "
        f"AND CloseDate <= {date_to} "
        "ORDER BY ACV__c DESC NULLS LAST "
        f"LIMIT {limit}"
    )


def fetch_open_opps(
    *,
    date_from: str = DEFAULT_FETCH_FROM,
    date_to: str = DEFAULT_FETCH_TO,
    limit: int = DEFAULT_FETCH_LIMIT,
) -> list[OppRecord]:
    """Pull open Opportunities in the horizon window, parsed into OppRecord.

    Retries once without `ICP_Score__c` if SF reports `INVALID_FIELD` for it —
    the ICP pillar falls back to its proxy path and the Deal Details sheet
    shows `proxy-capped` instead of the raw score.
    """
    query = build_open_pipeline_soql(date_from=date_from, date_to=date_to, limit=limit)
    try:
        result = salesforce_mcp.soql_query(query, limit=limit)
    except salesforce_mcp.SalesforceError as e:
        if "INVALID_FIELD" in str(e) and _ICP_COLUMN in str(e):
            log.warning(
                "fetch_open_opps: ICP_Score__c missing from Opportunity; "
                "retrying without it and relying on proxy path"
            )
            query = build_open_pipeline_soql(
                date_from=date_from, date_to=date_to, limit=limit, include_icp=False
            )
            result = salesforce_mcp.soql_query(query, limit=limit)
        else:
            raise

    records = result.get("records", []) if isinstance(result, dict) else []
    return [_parse_record(row) for row in records]


# ------------------------------------------------------------------ parsing

def _parse_record(row: dict[str, Any]) -> OppRecord:
    products: dict[str, int] = {}
    for sf_field, canonical in PRODUCT_FIELDS.items():
        value = row.get(sf_field)
        if value is None:
            continue
        try:
            products[canonical] = int(value)
        except (TypeError, ValueError):
            continue

    account = row.get("Account") or {}
    owner = row.get("Owner") or {}
    owner_role = (owner.get("UserRole") or {}).get("Name") if isinstance(owner, dict) else None
    owner_manager = (owner.get("Manager") or {}).get("Name") if isinstance(owner, dict) else None

    contact_roles_payload = row.get("OpportunityContactRoles") or {}
    contact_roles_records = (
        contact_roles_payload.get("records", []) if isinstance(contact_roles_payload, dict) else []
    )

    return OppRecord(
        id=row["Id"],
        name=row.get("Name", ""),
        account_id=row.get("AccountId"),
        account_name=account.get("Name") if isinstance(account, dict) else None,
        account_website=account.get("Website") if isinstance(account, dict) else None,
        account_type=account.get("Type") if isinstance(account, dict) else None,
        owner_id=row.get("OwnerId"),
        owner_name=owner.get("Name") if isinstance(owner, dict) else None,
        owner_role=owner_role,
        owner_manager=owner_manager,
        stage=row.get("StageName", ""),
        is_closed=bool(row.get("IsClosed")),
        is_won=bool(row.get("IsWon")),
        amount=_coerce_float(row.get("Amount")),
        acv=_coerce_float(row.get("ACV__c")),
        fixed_arr=_coerce_float(row.get("Fixed_ARR__c")),
        locations=_coerce_int(row.get("Locations__c")),
        type=row.get("Type"),
        lead_source=row.get("LeadSource"),
        close_date=_coerce_date(row.get("CloseDate")),
        created_date=_coerce_datetime(row.get("CreatedDate")),
        last_activity_date=_coerce_date(row.get("LastActivityDate")),
        last_modified_date=_coerce_datetime(row.get("LastModifiedDate")),
        last_stage_change_date=_coerce_date(row.get("LastStageChangeDate")),
        days_since_stage_change=_coerce_int(row.get("D_T_Last_Stage_Change__c")),
        time_in_stage=_coerce_int(row.get("Time_in_Stage__c")),
        probability_sf=_coerce_float(row.get("Probability")),
        description=row.get("Description"),
        next_steps=row.get("Next_Steps__c"),
        next_step_date=_coerce_date(row.get("Next_Step_Date__c")),
        icp_score=_coerce_float(row.get(_ICP_COLUMN)),
        segment=row.get("Segment__c"),
        products=products,
        contact_roles=[_parse_contact_role(cr) for cr in contact_roles_records],
        raw=row,
    )


def _parse_contact_role(row: dict[str, Any]) -> ContactRole:
    contact = row.get("Contact") or {}
    return ContactRole(
        contact_id=row.get("ContactId", ""),
        name=contact.get("Name") if isinstance(contact, dict) else None,
        email=contact.get("Email") if isinstance(contact, dict) else None,
        title=contact.get("Title") if isinstance(contact, dict) else None,
        role=row.get("Role"),
        is_primary=bool(row.get("IsPrimary")),
    )


# ------------------------------------------------------------------ coercion

def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            return None


def _coerce_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
