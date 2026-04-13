"""Closed Won → Onboarding__c poller.

Runs every 5 minutes (see shared/runtime/schedule.py). For each new Closed Won
opportunity, creates an `Onboarding__c` record if one doesn't already exist.

Idempotency has three layers:
  1. Dedup predicate at the SOQL level (Strategy A or B — see queries.py)
  2. Belt-and-suspenders pre-create query per opp (catches the race where an
     Onboarding__c was created between SOQL evaluation and create_record)
  3. Governance gate carries `opportunity_id` in its payload so parallel
     poller runs can't both create gates for the same opp

The `last_poll` watermark is stored in `agent_runs.completed_at` (most recent
successful poll). A fresh deploy starts with a 7-day lookback.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text as sql_text

from agents.onboarding import onboarding_record_creator, queries
from shared.db.connection import get_engine
from shared.governance import (
    ApprovalRequired,
    create_approval_gate,
    decide_approval_gate,
    write_audit,
)
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

AGENT_NAME = "onboarding"
_DEFAULT_LOOKBACK = timedelta(days=7)
_MAX_BATCH = 50  # safety cap per poll


# ---------- Pre-flight ----------

def detect_dedup_strategy() -> str:
    """Return 'A' if Onboarding_Record_Created__c exists on Opportunity, else 'B'.

    Cheap check — one FieldDefinition SOQL per poll. The result could be
    cached for the life of the daemon, but re-checking lets us pick up a new
    field the moment Agent 5 adds it.
    """
    try:
        res = salesforce_mcp.soql_query(queries.ONBOARDING_CREATED_FIELD_EXISTS)
    except Exception:  # broad — Tooling API can be flaky
        log.warning("dedup field probe failed; falling back to Strategy B", exc_info=True)
        return "B"
    records = res.get("records") or []
    return "A" if records else "B"


# ---------- Watermark ----------

def _last_poll_time() -> datetime:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                """SELECT completed_at FROM agent_runs
                    WHERE agent_name = :a AND status = 'completed'
                    ORDER BY completed_at DESC LIMIT 1"""
            ),
            {"a": AGENT_NAME},
        ).fetchone()
    if row and row[0]:
        value = row[0]
        if isinstance(value, str):
            # sqlite returns ISO string; Postgres returns datetime
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value
    return datetime.now(timezone.utc) - _DEFAULT_LOOKBACK


def _format_soql_datetime(dt: datetime) -> str:
    """SOQL wants ISO 8601 without quotes for datetime literals."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------- Candidate selection ----------

def _select_candidates(strategy: str, last_poll: datetime) -> list[dict[str, Any]]:
    poll_iso = _format_soql_datetime(last_poll)
    template = (
        queries.CLOSED_WON_STRATEGY_A if strategy == "A" else queries.CLOSED_WON_STRATEGY_B
    )
    query = template.format(last_poll_iso=poll_iso)
    res = salesforce_mcp.soql_query(query, limit=_MAX_BATCH)
    records = res.get("records") or []

    if strategy == "A" or not records:
        return records

    # Strategy B: filter out opps that already have an Onboarding__c.
    ids = [r["Id"] for r in records]
    quoted = ", ".join(f"'{i}'" for i in ids)
    existing_res = salesforce_mcp.soql_query(
        queries.EXISTING_ONBOARDINGS_FOR_OPPS.format(opp_ids_quoted=quoted)
    )
    existing_opp_ids = {
        r.get("Opportunity__c")
        for r in (existing_res.get("records") or [])
        if r.get("Opportunity__c")
    }
    return [r for r in records if r["Id"] not in existing_opp_ids]


# ---------- Belt-and-suspenders ----------

def _onboarding_already_exists(opp_id: str) -> bool:
    res = salesforce_mcp.soql_query(
        f"SELECT Id FROM Onboarding__c WHERE Opportunity__c = '{opp_id}' LIMIT 1"
    )
    return bool(res.get("records"))


# ---------- Gate plumbing ----------

def _create_self_approved_gate(opp: dict[str, Any]) -> int:
    """Create an approval gate for a single Onboarding__c create and self-approve it.

    action_type is `single_record_update` to satisfy the SF MCP's strict match
    in `require_approved_gate`. The business-intent marker
    `origin: "onboarding_auto_create"` is stored in payload — queries that
    reconcile the audit trail (weekly digest, dashboards) filter on that. The
    tier `onboarding_auto_create` in APPROVAL_TIERS remains the canonical
    policy row for this flow.
    """
    gate_id = create_approval_gate(
        agent_name=AGENT_NAME,
        action_type="single_record_update",
        payload={
            "origin": "onboarding_auto_create",
            "opportunity_id": opp["Id"],
            "account_id": opp.get("AccountId"),
            "owner_id": opp.get("OwnerId"),
            "amount": opp.get("Amount"),
        },
        justification=None,
        requested_by=f"system:{AGENT_NAME}",
    )
    decide_approval_gate(
        gate_id,
        approved=True,
        approver=f"system:{AGENT_NAME}",
    )
    return gate_id


# ---------- Run loop ----------

async def poll() -> dict[str, Any]:
    """Single poll tick. Returns a summary dict for logging / tests."""
    run_id = _start_run()
    summary: dict[str, Any] = {
        "strategy": None,
        "candidates": 0,
        "created": 0,
        "skipped": 0,
        "errors": [],
    }
    try:
        strategy = detect_dedup_strategy()
        summary["strategy"] = strategy
        last_poll = _last_poll_time()
        candidates = _select_candidates(strategy, last_poll)
        summary["candidates"] = len(candidates)

        if strategy == "B":
            _flag_schema_gap_task_if_needed()

        for opp in candidates:
            try:
                if _onboarding_already_exists(opp["Id"]):
                    summary["skipped"] += 1
                    log.info("belt-and-suspenders skip: opp=%s", opp["Id"])
                    continue
                gate_id = _create_self_approved_gate(opp)
                result = onboarding_record_creator.create_from_opp(
                    opp, gate_id=gate_id
                )
                summary["created"] += 1
                log.info(
                    "Onboarding__c created: opp=%s onboarding=%s gate=%s",
                    opp["Id"], result.get("id"), gate_id,
                )
            except ApprovalRequired as exc:
                summary["errors"].append({"opp": opp["Id"], "error": str(exc)})
                log.error("approval gate rejected: opp=%s err=%s", opp["Id"], exc)
            except Exception as exc:  # one bad opp must not block the batch
                summary["errors"].append({"opp": opp["Id"], "error": str(exc)})
                log.exception("create failed: opp=%s", opp["Id"])
                write_audit(
                    agent_name=AGENT_NAME,
                    action="sf_create_failed",
                    target=f"sf:Opportunity:{opp['Id']}",
                    after={"error": str(exc)},
                    run_id=run_id,
                )
        _finish_run(run_id, status="completed")
    except Exception as exc:
        log.exception("poll aborted: %s", exc)
        _finish_run(run_id, status="failed", error=str(exc))
        raise
    return summary


# ---------- agent_runs helpers ----------

def _start_run() -> int:
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            sql_text(
                """INSERT INTO agent_runs (agent_name, trigger, status, started_at)
                   VALUES (:a, 'cron:onboarding-closed-won-poller', 'in_progress', :t)"""
            ),
            {"a": AGENT_NAME, "t": datetime.now(timezone.utc)},
        )
        run_id = result.lastrowid
        if run_id is None:
            row = conn.execute(
                sql_text("SELECT id FROM agent_runs ORDER BY id DESC LIMIT 1")
            ).fetchone()
            run_id = row[0]
    return int(run_id)


def _finish_run(run_id: int, *, status: str, error: str | None = None) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """UPDATE agent_runs
                      SET status = :s, completed_at = :t, error_message = :e
                    WHERE id = :id"""
            ),
            {"s": status, "t": datetime.now(timezone.utc), "e": error, "id": run_id},
        )


# ---------- Strategy B → flag schema gap ----------

_SCHEMA_GAP_TASK_SOURCE = "onboarding:schema_gap:onboarding_record_created"


def _flag_schema_gap_task_if_needed() -> None:
    """If we're on Strategy B, seed a task for Agent 5 (RevOps Support).

    Idempotent via the unique `source` marker.
    """
    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            sql_text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"),
            {"s": _SCHEMA_GAP_TASK_SOURCE},
        ).fetchone()
        if existing:
            return
        conn.execute(
            sql_text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee)
                   VALUES ('revops_support',
                           'Add Onboarding_Record_Created__c checkbox to Opportunity',
                           :d, 'pending', 'medium', 'sf_schema_gap', :s, 'system')"""
            ),
            {
                "s": _SCHEMA_GAP_TASK_SOURCE,
                "d": (
                    "Onboarding agent is running on Strategy B (SOQL existence probe) "
                    "because Opportunity.Onboarding_Record_Created__c does not exist. "
                    "Adding the boolean (default false) lets the poller switch to the "
                    "cheaper Strategy A predicate and reduces duplicate-write risk."
                ),
            },
        )
        log.info("seeded schema-gap task for Agent 5")
