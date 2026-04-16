"""Governance — approval tiers, rate limits, audit writes.

Every high-blast-radius action routes through here. Phase 0 covers the
enforcement surface; Phase 1 specialists consume these primitives.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Tier:
    gate: str | None
    approver: str | None = None
    range: tuple[int, int | None] | None = None
    requires_justification: bool = False
    review_window: str | None = None
    cooldown_hours: int | None = None


_SF_SCHEMA_CHANGE = Tier(gate="full_workflow", approver="o_only", requires_justification=True)

APPROVAL_TIERS: dict[str, Tier] = {
    "read_query": Tier(gate=None),
    "single_record_update": Tier(gate="auto_notify", approver=None),
    "bulk_update_small": Tier(gate="slack_button", approver="o_or_dept_head", range=(2, 99)),
    "bulk_update_large": Tier(
        gate="slack_explicit", approver="o_only", range=(100, None), requires_justification=True
    ),
    # Kept as alias for backward compat; new callers should use the create/modify/delete trio.
    "sf_schema_change": _SF_SCHEMA_CHANGE,
    "sf_schema_create": _SF_SCHEMA_CHANGE,
    "sf_schema_modify": _SF_SCHEMA_CHANGE,
    "sf_schema_delete": Tier(
        gate="dual_approval_cooldown",
        approver="o_only",
        requires_justification=True,
        cooldown_hours=24,
    ),
    "sf_schema_delete_confirm": Tier(
        gate="slack_explicit", approver="o_only", requires_justification=False
    ),
    "user_provisioning": Tier(gate="slack_explicit", approver="o_only"),
    "permission_grant": Tier(
        gate="slack_explicit", approver="o_only", requires_justification=True
    ),
    "license_deactivation": Tier(gate="slack_explicit", approver="o_only"),
    "apex_flow_deploy": Tier(gate="sandbox_then_approve", approver="o_only"),
    "outbound_sequence": Tier(
        gate="rate_limit_and_review", approver="o_only", review_window="08:00_daily"
    ),
    "commission_adjustment": Tier(gate="explicit", approver="o_only"),
    "customer_facing_comms": Tier(gate="draft_review", approver="dept_head"),
    "mark_churned": Tier(gate="dual_approval", approver="jackie_and_o"),
    # Phase 1 — Agent 3 (Onboarding). onboarding_auto_create records every
    # Onboarding__c auto-created from a Closed Won opp with full audit but
    # without a blocking human step; self-approved via auto_approve_gate().
    "onboarding_auto_create": Tier(gate="auto_approve", approver="system"),
    "csm_reassignment": Tier(gate="slack_button", approver="jackie_or_o"),
    "onboarding_complete": Tier(gate="slack_button", approver="jackie_or_o"),
    "skip_milestone": Tier(
        gate="slack_explicit", approver="jackie_or_o", requires_justification=True
    ),
    # Phase 1 — Agent 6 (SLT Revenue Metrics). Every SLT-facing deliverable
    # (daily 8:30, Friday review, on-demand forecast/movers/scorecards) drafts
    # to O's DM only; O forwards manually. No auto-channel fanout.
    "slt_draft_review": Tier(gate="slack_button", approver="o_only"),
    # Phase 1 — Agent 4 (CS). Dual-approval Mark Churned flow: Gate A is
    # Jackie's request, Gate B is O's confirmation, the SF write only
    # happens after both are approved. `cs_churn_outreach` is a
    # draft-review gate approved by Jackie; the agent posts the approved
    # draft to CSM Slack (no direct customer write).
    "cs_churn_outreach": Tier(gate="draft_review", approver="dept_head"),
    "mark_churned_request": Tier(
        gate="slack_button", approver="jackie_or_dept_head", requires_justification=True
    ),
    "mark_churned_confirm": Tier(
        gate="slack_button", approver="o_only", requires_justification=True
    ),
    # Phase 1 — Agent 1 (Top of Funnel). Used when an SDR / O overrides a
    # suppression hit (e.g., re-engaging a former customer). Requires written
    # justification to preserve the audit trail.
    "suppression_override": Tier(
        gate="slack_button", approver="o_or_dept_head", requires_justification=True
    ),
}

RATE_LIMITS: dict[str, int] = {
    "nooks_sequences_daily": 50,
    "sf_lead_creation_daily": 200,
    "sf_bulk_update_hourly": 500,
    "renewal_outreach_daily": 10,
    # RevOps Support (Agent 5) — Phase 1
    "revops_bulk_update_daily": 500,
    "revops_schema_changes_weekly": 10,
    "revops_describe_calls_hourly": 200,
    "revops_metadata_deploy_daily": 5,
}

# Buckets that WARN instead of raising when over limit. Used for guardrails
# that should notify O without blocking in-flight work (e.g., weekly schema-
# change velocity). Hard limits still raise RateLimitExceeded.
SOFT_LIMIT_BUCKETS: set[str] = {
    "revops_schema_changes_weekly",
}


class ApprovalRequired(Exception):
    """Raised when an action needs approval but none was provided / approved."""


class RateLimitExceeded(Exception):
    pass


def classify_bulk_update(count: int) -> str:
    if count <= 1:
        return "single_record_update"
    if count < 100:
        return "bulk_update_small"
    return "bulk_update_large"


def create_approval_gate(
    *,
    agent_name: str,
    action_type: str,
    payload: dict[str, Any],
    justification: str | None,
    requested_by: str = "system",
    ttl_hours: int = 24,
) -> int:
    tier = APPROVAL_TIERS.get(action_type)
    if tier and tier.requires_justification and not justification:
        raise ApprovalRequired(f"{action_type} requires written justification")

    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=ttl_hours)
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO approval_gates
                    (agent_name, action_type, payload, justification, requested_by,
                     status, requested_at, expires_at)
                VALUES (:agent, :action, :payload, :just, :by, 'pending', :now, :exp)
                """
            ),
            {
                "agent": agent_name,
                "action": action_type,
                "payload": json.dumps(payload),
                "just": justification,
                "by": requested_by,
                "now": now,
                "exp": expires,
            },
        )
        gate_id = result.lastrowid
        if gate_id is None:
            row = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()
            gate_id = row[0]
    log.info("approval_gate created id=%s action=%s agent=%s", gate_id, action_type, agent_name)
    return int(gate_id)


def get_approval_gate(gate_id: int) -> dict[str, Any] | None:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT * FROM approval_gates WHERE id = :id"), {"id": gate_id}
        ).mappings().fetchone()
        return dict(row) if row else None


def decide_approval_gate(gate_id: int, *, approved: bool, approver: str) -> None:
    """Record a decision on a gate. Writes audit('gate_decided').

    For `dual_approval_cooldown` tiers (e.g. `sf_schema_delete`), approval
    transitions to `approved_primary` with `cooldown_until = now + cooldown_hours`.
    A scheduled poller creates the confirmation gate once cooldown elapses; the
    confirmation gate itself (e.g. `sf_schema_delete_confirm`) uses the normal
    slack_explicit flow and transitions straight to `approved`.

    For dual-approval tiers (approver='jackie_and_o', e.g. `mark_churned`):
    each call appends to the `approvals` JSON list. Status flips to `approved`
    only when 2+ distinct approvers have approved with no intervening
    rejection. A single rejection from any approver flips the gate to
    `rejected` immediately.
    """
    now = datetime.now(timezone.utc)
    engine = get_engine()
    audit_payload: dict[str, Any] = {"approved": approved, "approver": approver}
    final_status: str | None = None
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT action_type, status, approvals FROM approval_gates WHERE id = :id"),
            {"id": gate_id},
        ).fetchone()
        if row is None or row[1] != "pending":
            return

        action_type = row[0]
        existing = json.loads(row[2]) if row[2] else []
        tier = APPROVAL_TIERS.get(action_type)
        cooldown_hours = tier.cooldown_hours if tier else None
        is_dual = bool(tier and tier.approver == "jackie_and_o")

        existing.append(
            {"approver": approver, "approved": approved, "decided_at": now.isoformat()}
        )
        approvals_json = json.dumps(existing)

        if not approved:
            status = "rejected"
            cooldown_until = None
        elif is_dual:
            any_reject = any(not a["approved"] for a in existing)
            distinct_yes = {a["approver"] for a in existing if a["approved"]}
            if any_reject:
                status = "rejected"
                cooldown_until = None
            elif len(distinct_yes) >= 2:
                status = "approved"
                cooldown_until = None
            else:
                conn.execute(
                    text(
                        "UPDATE approval_gates SET approvals = :ap, decided_at = :now"
                        " WHERE id = :id AND status = 'pending'"
                    ),
                    {"ap": approvals_json, "now": now, "id": gate_id},
                )
                audit_payload["needs_more_approvers"] = True
                final_status = "pending"
        elif cooldown_hours:
            status = "approved_primary"
            cooldown_until = now + timedelta(hours=cooldown_hours)
        else:
            status = "approved"
            cooldown_until = None

        if final_status is None:
            conn.execute(
                text(
                    """
                    UPDATE approval_gates
                       SET status = :s, approved_by = :a, decided_at = :now,
                           cooldown_until = :cd, approvals = :ap
                     WHERE id = :id AND status = 'pending'
                    """
                ),
                {
                    "s": status,
                    "a": approver,
                    "now": now,
                    "cd": cooldown_until,
                    "ap": approvals_json,
                    "id": gate_id,
                },
            )
            final_status = status

    audit_payload["final_status"] = final_status
    # `target` carries the gate id; we deliberately skip the approval_gate_id FK
    # so test fixtures that DELETE approval_gates before audit_log don't break.
    write_audit(
        agent_name="governance",
        action="gate_decided",
        target=f"gate_{gate_id}",
        after=audit_payload,
    )


def auto_approve_gate(gate_id: int, *, approver: str = "system") -> None:
    """Atomically approve a pending gate whose tier is gate='auto_approve'.

    Guards on the tier type (not a specific action_type) so any future
    auto-approve tier inherits safe usage. Raises ApprovalRequired if the gate
    is missing, already decided, or belongs to a non-auto_approve tier — the
    latter prevents misuse (e.g. bypassing a slack_button gate for a dept head
    decision).
    """
    gate = get_approval_gate(gate_id)
    if not gate:
        raise ApprovalRequired(f"auto_approve_gate: gate {gate_id} not found")
    tier = APPROVAL_TIERS.get(gate["action_type"])
    if not tier or tier.gate != "auto_approve":
        raise ApprovalRequired(
            f"auto_approve_gate refused: gate {gate_id} "
            f"action_type={gate['action_type']} is not an auto_approve tier"
        )
    decide_approval_gate(gate_id, approved=True, approver=approver)


def require_approved_gate(gate_id: int | None, *, action_type: str) -> dict[str, Any]:
    if gate_id is None:
        raise ApprovalRequired(f"{action_type} requires an approved approval_gate_id")
    gate = get_approval_gate(gate_id)
    if not gate:
        raise ApprovalRequired(f"approval_gate {gate_id} not found")
    if gate["status"] != "approved":
        raise ApprovalRequired(
            f"approval_gate {gate_id} status={gate['status']} (required: approved)"
        )
    if gate["action_type"] != action_type:
        raise ApprovalRequired(
            f"approval_gate {gate_id} is for {gate['action_type']}, not {action_type}"
        )
    return gate


def check_rate_limit(
    bucket: str,
    window_seconds: int = 86400,
    *,
    mode: str | None = None,
) -> int:
    """Atomic increment of bucket's current window.

    mode="hard" (default for most buckets): raise RateLimitExceeded when over.
    mode="soft": log WARN and return the count without raising.
    mode=None:  resolved from SOFT_LIMIT_BUCKETS (soft) else "hard".

    `window_seconds` selects the window granularity:
      >= 604800 (week): YYYY-W## ISO week
      >= 86400  (day):  midnight UTC of current day
      else (hour):      top of current hour
    """
    limit = RATE_LIMITS.get(bucket)
    if limit is None:
        raise ValueError(f"Unknown rate limit bucket: {bucket}")
    resolved_mode = mode or ("soft" if bucket in SOFT_LIMIT_BUCKETS else "hard")
    if resolved_mode not in ("hard", "soft"):
        raise ValueError(f"mode must be 'hard' or 'soft', got {resolved_mode!r}")

    now = datetime.now(timezone.utc)
    if window_seconds >= 604800:
        iso_year, iso_week, _ = now.isocalendar()
        window = datetime.fromisocalendar(iso_year, iso_week, 1).replace(tzinfo=timezone.utc)
    elif window_seconds >= 86400:
        window = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        window = now.replace(minute=0, second=0, microsecond=0)

    engine = get_engine()
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT id, count FROM rate_limits WHERE bucket = :b AND window_start = :w"),
            {"b": bucket, "w": window},
        ).fetchone()
        if existing:
            count = existing[1] + 1
            if count > limit:
                if resolved_mode == "hard":
                    raise RateLimitExceeded(f"{bucket}: {count}/{limit}")
                log.warning("rate_limit SOFT breach bucket=%s count=%s/%s", bucket, count, limit)
            conn.execute(
                text("UPDATE rate_limits SET count = :c WHERE id = :id"),
                {"c": count, "id": existing[0]},
            )
            return count
        conn.execute(
            text(
                """INSERT INTO rate_limits (bucket, count, window_start, limit_value)
                   VALUES (:b, 1, :w, :l)"""
            ),
            {"b": bucket, "w": window, "l": limit},
        )
        return 1


def write_audit(
    *,
    agent_name: str,
    action: str,
    target: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    approval_gate_id: int | None = None,
    rate_limit_bucket: str | None = None,
    run_id: int | None = None,
) -> int | None:
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO audit_log
                    (agent_name, action, target, before_value, after_value,
                     approval_gate_id, rate_limit_bucket, run_id)
                VALUES (:a, :act, :t, :b, :af, :g, :rb, :r)
                """
            ),
            {
                "a": agent_name,
                "act": action,
                "t": target,
                "b": json.dumps(before) if before else None,
                "af": json.dumps(after) if after else None,
                "g": approval_gate_id,
                "rb": rate_limit_bucket,
                "r": run_id,
            },
        )
        audit_id = result.lastrowid
        if audit_id is None:
            row = conn.execute(
                text("SELECT id FROM audit_log ORDER BY id DESC LIMIT 1")
            ).fetchone()
            audit_id = row[0] if row else None
        return int(audit_id) if audit_id is not None else None
