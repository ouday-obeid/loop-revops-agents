"""Canned SOQL queries for @oo revops-support commands.

Each function returns a dict with:
  - records: list[dict]   — raw SOQL rows
  - text:    str          — markdown table (Slack text fallback)
  - blocks:  list[dict]   — Block Kit blocks for rich rendering

Field assumptions flagged TODO(verify) — confirm against live describe before
shipping Week 1 Day 5 milestone. TLO linkage lives on the `Top_Level_Organization__c`
custom object with `TLO__c` lookup on Account/Opportunity per sf-admin knowledge
base; if Loop AI's actual field names differ, update here.
"""
from __future__ import annotations

from typing import Any, Callable

from agents.revops_support.query import soql_engine


def _mk_text_table(rows: list[dict[str, Any]], columns: list[str], title: str) -> str:
    if not rows:
        return f"*{title}* — no results."
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    body = "\n".join(
        " | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns) for r in rows
    )
    return f"*{title}*\n```\n{header}\n{sep}\n{body}\n```"


def _mk_blocks(text: str) -> list[dict[str, Any]]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def _package(records: list[dict[str, Any]], columns: list[str], title: str) -> dict[str, Any]:
    text = _mk_text_table(records, columns, title)
    return {"records": records, "text": text, "blocks": _mk_blocks(text)}


# ---------- 1. pipeline_by_stage ----------

def pipeline_by_stage() -> dict[str, Any]:
    q = (
        "SELECT StageName, COUNT(Id) opp_count, SUM(Amount) total_amount "
        "FROM Opportunity WHERE IsClosed = false GROUP BY StageName"
    )
    result = soql_engine.run(q, default_limit=100)
    records = result.get("records", [])
    # aggregate query rows use anon fields — normalize
    rows = [
        {
            "stage": r.get("StageName"),
            "count": r.get("opp_count") or r.get("expr0"),
            "amount": r.get("total_amount") or r.get("expr1") or 0,
        }
        for r in records
    ]
    return _package(rows, ["stage", "count", "amount"], "Pipeline by Stage")


# ---------- 2. stale_opportunities ----------

def stale_opportunities(days: int = 30) -> dict[str, Any]:
    q = (
        "SELECT Id, Name, StageName, Amount, LastModifiedDate, Owner.Name "
        "FROM Opportunity "
        f"WHERE IsClosed = false AND LastModifiedDate < LAST_N_DAYS:{days} "
        "ORDER BY LastModifiedDate ASC"
    )
    result = soql_engine.run(q, default_limit=50)
    rows = [
        {
            "id": r["Id"],
            "name": r["Name"],
            "stage": r["StageName"],
            "amount": r.get("Amount") or 0,
            "last_modified": r["LastModifiedDate"],
            "owner": (r.get("Owner") or {}).get("Name"),
        }
        for r in result.get("records", [])
    ]
    return _package(
        rows,
        ["name", "stage", "amount", "owner", "last_modified"],
        f"Stale Opportunities (>{days}d)",
    )


# ---------- 3. tlos_with_no_opps ----------

def tlos_with_no_opps() -> dict[str, Any]:
    # TODO(verify): confirm Top_Level_Organization__c + TLO__c linkage via describe.
    q = (
        "SELECT Id, Name FROM Top_Level_Organization__c "
        "WHERE Id NOT IN (SELECT TLO__c FROM Opportunity WHERE TLO__c != null) "
        "ORDER BY Name"
    )
    result = soql_engine.run(q, default_limit=50)
    rows = [{"id": r["Id"], "name": r["Name"]} for r in result.get("records", [])]
    return _package(rows, ["name", "id"], "TLOs with No Opportunities")


# ---------- 4. opps_missing_products ----------

def opps_missing_products() -> dict[str, Any]:
    q = (
        "SELECT Id, Name, StageName, Amount, CloseDate FROM Opportunity "
        "WHERE IsWon = true AND Id NOT IN (SELECT OpportunityId FROM OpportunityLineItem) "
        "ORDER BY CloseDate DESC"
    )
    result = soql_engine.run(q, default_limit=50)
    rows = [
        {
            "id": r["Id"],
            "name": r["Name"],
            "stage": r["StageName"],
            "amount": r.get("Amount") or 0,
            "close_date": r["CloseDate"],
        }
        for r in result.get("records", [])
    ]
    return _package(
        rows, ["name", "stage", "amount", "close_date"], "Won Opportunities Missing Products"
    )


# ---------- 5. accounts_with_no_tlo ----------

def accounts_with_no_tlo() -> dict[str, Any]:
    # TODO(verify): TLO__c exists on Account per sf-admin object model docs.
    q = "SELECT Id, Name, Industry FROM Account WHERE TLO__c = null ORDER BY Name"
    result = soql_engine.run(q, default_limit=100)
    rows = [
        {"id": r["Id"], "name": r["Name"], "industry": r.get("Industry")}
        for r in result.get("records", [])
    ]
    return _package(rows, ["name", "industry", "id"], "Accounts Missing TLO Linkage")


# ---------- 6. duplicate_contacts_by_email ----------

def duplicate_contacts_by_email() -> dict[str, Any]:
    q = (
        "SELECT Email, COUNT(Id) dup_count FROM Contact "
        "WHERE Email != null GROUP BY Email HAVING COUNT(Id) > 1 "
        "ORDER BY COUNT(Id) DESC"
    )
    result = soql_engine.run(q, default_limit=100)
    rows = [
        {
            "email": r.get("Email"),
            "count": r.get("dup_count") or r.get("expr0"),
        }
        for r in result.get("records", [])
    ]
    return _package(rows, ["email", "count"], "Duplicate Contacts by Email")


# ---------- 7. active_users_with_login ----------

def active_users_with_login(days: int = 30) -> dict[str, Any]:
    q = (
        "SELECT Id, Name, Username, LastLoginDate, Profile.Name "
        "FROM User WHERE IsActive = true "
        f"AND LastLoginDate >= LAST_N_DAYS:{days} "
        "ORDER BY LastLoginDate DESC"
    )
    result = soql_engine.run(q, default_limit=100)
    rows = [
        {
            "name": r["Name"],
            "username": r["Username"],
            "profile": (r.get("Profile") or {}).get("Name"),
            "last_login": r.get("LastLoginDate"),
        }
        for r in result.get("records", [])
    ]
    return _package(
        rows, ["name", "username", "profile", "last_login"], f"Active Users (login < {days}d)"
    )


# ---------- 8. validation_rule_violations ----------

def validation_rule_violations(object_name: str) -> dict[str, Any]:
    """List active validation rules for an object (violations are runtime-only in SF;
    this surfaces which rules are in force so a bulk update plan can pre-check them).
    """
    if not object_name.replace("_", "").replace("__c", "").isalnum():
        raise soql_engine.SOQLError(f"invalid object name: {object_name}")
    q = (
        "SELECT Id, ValidationName, Active, Description, ErrorMessage "
        f"FROM ValidationRule WHERE EntityDefinition.QualifiedApiName = '{object_name}' "
        "AND Active = true"
    )
    # Tooling API needed for ValidationRule — soql_engine uses data API, so call
    # salesforce_mcp directly with tooling flag via a bespoke describe_flow-style path.
    from shared.mcp import salesforce_mcp as sfm
    result = sfm._sf("data", "query", "--query", q, "--use-tooling-api", intent="read")
    rows = [
        {
            "name": r["ValidationName"],
            "active": r["Active"],
            "description": (r.get("Description") or "")[:80],
            "error": (r.get("ErrorMessage") or "")[:80],
        }
        for r in result.get("records", [])
    ]
    return _package(
        rows, ["name", "active", "description", "error"],
        f"Active Validation Rules on {object_name}",
    )


# ---------- Registry ----------

REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "pipeline_by_stage": pipeline_by_stage,
    "stale_opportunities": stale_opportunities,
    "tlos_with_no_opps": tlos_with_no_opps,
    "opps_missing_products": opps_missing_products,
    "accounts_with_no_tlo": accounts_with_no_tlo,
    "duplicate_contacts_by_email": duplicate_contacts_by_email,
    "active_users_with_login": active_users_with_login,
    "validation_rule_violations": validation_rule_violations,
}
