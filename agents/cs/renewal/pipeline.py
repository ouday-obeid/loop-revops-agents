"""T-120 renewal pipeline sweep.

Finds accounts whose current contract ends in 118-122 days (3-day tolerance
window — catches missed runs without double-creating) and ensures a `Renewal`
Opportunity exists. Idempotent via:

  1. SOQL pre-check: SELECT Id FROM Opportunity WHERE AccountId = :a AND
     Type = 'Renewal' AND Zen_Contract_End_Date__c = :end_date
  2. cs_renewal_state double-guard (PK opportunity_id): updated after create.

Stage fallback: at first run per process, describe Opportunity.StageName
picklist. If `Renewal Outreach` is missing, use the first active stage as a
fallback and:
  - mark cs_renewal_state.provisional = 1
  - open an idempotent revops_support task asking for the picklist value

SF writes use the self-approved `single_record_update` tier, mirroring the
onboarding auto-create pattern (closed_won_poller._create_self_approved_gate).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import create_approval_gate, decide_approval_gate

log = logging.getLogger(__name__)

AGENT_NAME = "cs"
WINDOW_DAYS = 120
WINDOW_TOLERANCE = 2  # ±2 → 118..122 inclusive
PREFERRED_STAGE = "Renewal Outreach"


def _find_renewal_stage(sf_mcp: Any) -> tuple[str, bool]:
    """Return (stage_name, is_fallback)."""
    try:
        desc = sf_mcp.describe_sobject("Opportunity")
    except Exception as e:
        log.warning("describe_sobject failed: %s — using preferred stage", e)
        return PREFERRED_STAGE, False

    stage_field = next(
        (f for f in desc.get("fields", []) if f.get("name") == "StageName"),
        None,
    )
    if not stage_field:
        return PREFERRED_STAGE, False
    values = [v.get("value") for v in stage_field.get("picklistValues", []) if v.get("active")]
    if PREFERRED_STAGE in values:
        return PREFERRED_STAGE, False
    fallback = values[0] if values else PREFERRED_STAGE
    _open_stage_task(fallback)
    return fallback, True


def _open_stage_task(fallback_stage: str) -> None:
    source = "cs:renewal_pipeline:missing_stage"
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
                                      category, source, assignee)
                   VALUES ('revops_support',
                           'Add Renewal Outreach to Opportunity.StageName',
                           :d, 'pending', 'high', 'sf_config', :s, 'system')"""
            ),
            {
                "s": source,
                "d": (
                    f"CS renewal pipeline cannot find picklist value 'Renewal Outreach' "
                    f"on Opportunity.StageName. Using fallback `{fallback_stage}` and "
                    "flagging those opportunities as provisional until the picklist is "
                    "added. Once added, CS will reconcile provisional opps."
                ),
            },
        )


def _find_due_opps(sf_mcp: Any, now: datetime) -> list[dict[str, Any]]:
    lo = (now + timedelta(days=WINDOW_DAYS - WINDOW_TOLERANCE)).date().isoformat()
    hi = (now + timedelta(days=WINDOW_DAYS + WINDOW_TOLERANCE)).date().isoformat()
    # Primary trigger: open opps (any type) with contract end date inside window.
    q = (
        "SELECT Id, AccountId, Account.Name, OwnerId, Amount, "
        "Zen_Contract_End_Date__c "
        "FROM Opportunity "
        f"WHERE Zen_Contract_End_Date__c >= {lo} "
        f"AND Zen_Contract_End_Date__c <= {hi} "
        "AND IsClosed = false"
    )
    r = sf_mcp.soql_query(q, limit=500)
    return r.get("records", []) or []


def _existing_renewal(sf_mcp: Any, account_id: str, end_date: str) -> str | None:
    q = (
        f"SELECT Id FROM Opportunity "
        f"WHERE AccountId = '{account_id}' "
        f"AND Type = 'Renewal' "
        f"AND Zen_Contract_End_Date__c = {end_date} "
        f"LIMIT 1"
    )
    r = sf_mcp.soql_query(q, limit=1)
    records = r.get("records") or []
    return records[0].get("Id") if records else None


def _state_exists(opportunity_id: str) -> bool:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT 1 FROM cs_renewal_state WHERE opportunity_id = :o LIMIT 1"),
            {"o": opportunity_id},
        ).fetchone()
    return bool(row)


def _persist_state(
    opportunity_id: str,
    account_id: str,
    stage: str,
    end_date: str,
    *,
    provisional: bool,
) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT OR IGNORE INTO cs_renewal_state
                     (opportunity_id, account_id, stage, contract_end_date, provisional)
                   VALUES (:o, :a, :st, :ed, :p)"""
            ),
            {
                "o": opportunity_id,
                "a": account_id,
                "st": stage,
                "ed": end_date,
                "p": 1 if provisional else 0,
            },
        )


def _self_approved_gate(payload: dict[str, Any]) -> int:
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="single_record_update",
        payload={"origin": "cs_renewal_create", **payload},
        justification=None,
        requested_by=f"system:{AGENT_NAME}",
    )
    decide_approval_gate(gate_id, approved=True, approver=f"system:{AGENT_NAME}")
    return gate_id


def _create_renewal_opp(
    sf_mcp: Any,
    source_opp: dict[str, Any],
    stage: str,
    end_date: str,
    *,
    dry_run: bool,
) -> str | None:
    account_name = (source_opp.get("Account") or {}).get("Name") or source_opp["AccountId"]
    year = end_date[:4] if end_date else str(datetime.now(timezone.utc).year)
    fields = {
        "Name": f"{account_name} Renewal {year}",
        "AccountId": source_opp["AccountId"],
        "Type": "Renewal",
        "StageName": stage,
        "CloseDate": end_date,
        "Amount": source_opp.get("Amount"),
        "OwnerId": source_opp.get("OwnerId"),
        "Zen_Contract_End_Date__c": end_date,
    }
    if dry_run:
        log.info("dry_run: would create renewal opp %s", fields)
        return None
    gate_id = _self_approved_gate(
        {"account_id": source_opp["AccountId"], "end_date": end_date, "stage": stage}
    )
    result = sf_mcp.create_record(
        "Opportunity", fields, agent_name=AGENT_NAME, approval_gate_id=gate_id
    )
    return result.get("id") or result.get("Id")


async def run_sweep(
    *,
    sf_mcp: Any | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Single sweep tick. Returns counters for telemetry / tests."""
    now = now or datetime.now(timezone.utc)
    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf

    counters = {"candidates": 0, "created": 0, "skipped": 0, "provisional": 0, "errors": 0}

    stage, is_fallback = _find_renewal_stage(sf_mcp)
    opps = _find_due_opps(sf_mcp, now)
    counters["candidates"] = len(opps)

    for opp in opps:
        try:
            end = opp.get("Zen_Contract_End_Date__c")
            acct = opp.get("AccountId")
            if not end or not acct:
                counters["skipped"] += 1
                continue
            existing = _existing_renewal(sf_mcp, acct, end)
            if existing:
                _persist_state(existing, acct, stage, end, provisional=is_fallback)
                counters["skipped"] += 1
                continue
            new_id = _create_renewal_opp(sf_mcp, opp, stage, end, dry_run=dry_run)
            if dry_run or not new_id:
                counters["skipped"] += 1
                continue
            if _state_exists(new_id):
                counters["skipped"] += 1
                continue
            _persist_state(new_id, acct, stage, end, provisional=is_fallback)
            counters["created"] += 1
            if is_fallback:
                counters["provisional"] += 1
        except Exception as e:
            log.exception("renewal sweep failed for opp %s: %s", opp.get("Id"), e)
            counters["errors"] += 1

    log.info("cs-renewal-pipeline complete: %s", counters)
    return counters
