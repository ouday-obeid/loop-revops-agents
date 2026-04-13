"""Sequence Enroller — writes to the SF object that Nooks mirrors.

Nooks is a read-only mirror of Salesforce, so "sequence enrollment" means
creating records in the SF object Nooks watches for cadence membership. Exact
object name comes from `NOOKS_CADENCE_SF_OBJECT` env var (defaults to
`CampaignMember`; confirmed with O at D1 kickoff, documented in RUNBOOK).

Governance surface:
  - APPROVAL_TIERS["outbound_sequence"] — rate_limit_and_review, o_only,
    08:00_daily review window (declared in shared.governance).
  - RATE_LIMITS["nooks_sequences_daily"] = 50 — the 51st enrollment today
    raises RateLimitExceeded BEFORE the SF write (atomic).

Flow (enroll_batch):
  1. require_approved_gate(gate_id, action_type="outbound_sequence")
  2. For each lead_id:
       check_rate_limit("nooks_sequences_daily")   # raises on 51st
       create_record(NOOKS_CADENCE_SF_OBJECT, ...) # carries approval_gate_id
       write tof_sequence_enrollments              # local audit
  3. Return {enrolled: N, failed: [...], rate_limit_hit: bool}

Slack surface:
  `@oo tof queue status`        → list pending outbound_sequence gates
  `@oo tof queue approve <id>`  → approve via handler (O only)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from shared.governance import (
    RateLimitExceeded,
    check_rate_limit,
    decide_approval_gate,
    get_approval_gate,
    require_approved_gate,
)
from shared.secrets import get_config

from agents.top_of_funnel.state import get_state_engine

log = logging.getLogger(__name__)

_AGENT_NAME = "top_of_funnel"
_RATE_BUCKET = "nooks_sequences_daily"
_GATE_ACTION = "outbound_sequence"


# ------------------------------------------------- local enrollment audit

_ENROLLMENTS_DDL = """
CREATE TABLE IF NOT EXISTS tof_sequence_enrollments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id TEXT NOT NULL,
    sequence_id TEXT NOT NULL,
    sf_record_id TEXT,
    sf_object TEXT NOT NULL,
    approval_gate_id INTEGER NOT NULL,
    enrolled_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'enrolled',  -- enrolled | failed | rate_limited
    error_message TEXT
)
"""


def _ensure_enrollments_table() -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text(_ENROLLMENTS_DDL))


def _cadence_sobject() -> str:
    """SF object Nooks mirrors for cadence membership. Defaults to
    CampaignMember; override with NOOKS_CADENCE_SF_OBJECT."""
    return get_config("NOOKS_CADENCE_SF_OBJECT") or "CampaignMember"


# -------------------------------------------------------- enroll


def enroll_batch(
    lead_ids: list[str],
    sequence_id: str,
    *,
    approval_gate_id: int,
    create_fn: Callable[..., dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Enroll leads into a Nooks cadence (via the SF mirror object).

    Rate-limit is checked per lead BEFORE the SF write so the 51st enrollment
    today raises `RateLimitExceeded` without leaving an orphan SF row. We
    stop at the first rate-limit; remaining leads are left for tomorrow.

    Raises:
        ApprovalRequired  — gate missing / wrong action_type / not approved
        RateLimitExceeded — 51st attempt of the day (audited)
    """
    _ensure_enrollments_table()
    require_approved_gate(approval_gate_id, action_type=_GATE_ACTION)

    if create_fn is None:
        from shared.mcp.salesforce_mcp import create_record as _sf_create
        def _default_create(sobject: str, fields: dict[str, Any], **kw: Any) -> dict[str, Any]:
            return _sf_create(
                sobject,
                fields,
                agent_name=_AGENT_NAME,
                approval_gate_id=approval_gate_id,
            )
        create_fn = _default_create

    sobject = _cadence_sobject()
    enrolled: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    rate_hit = False

    for lead_id in lead_ids:
        try:
            check_rate_limit(_RATE_BUCKET)
        except RateLimitExceeded as exc:
            rate_hit = True
            _record_enrollment(
                lead_id=lead_id, sequence_id=sequence_id, sf_record_id=None,
                sobject=sobject, approval_gate_id=approval_gate_id,
                status="rate_limited", error=str(exc),
            )
            log.warning("sequence_enroller: rate limit hit at lead %s — %s", lead_id, exc)
            break  # stop processing; leave remainder for tomorrow

        fields = {
            "LeadId": lead_id,
            "SequenceId__c": sequence_id,  # Custom field on CampaignMember (Phase 0)
            # If NOOKS_CADENCE_SF_OBJECT is CampaignMember, CampaignId is required;
            # callers should pass sequence_id as the CampaignId.
            "CampaignId": sequence_id if sobject == "CampaignMember" else None,
        }
        # Drop None fields so CampaignMember doesn't reject on missing CampaignId
        # for non-Campaign objects.
        fields = {k: v for k, v in fields.items() if v is not None}

        try:
            result = create_fn(sobject, fields)
        except Exception as exc:  # noqa: BLE001
            _record_enrollment(
                lead_id=lead_id, sequence_id=sequence_id, sf_record_id=None,
                sobject=sobject, approval_gate_id=approval_gate_id,
                status="failed", error=str(exc),
            )
            failed.append({"lead_id": lead_id, "error": str(exc)})
            log.exception("sequence_enroller: create failed for %s", lead_id)
            continue

        sf_id = (result or {}).get("id") or (result or {}).get("Id")
        _record_enrollment(
            lead_id=lead_id, sequence_id=sequence_id, sf_record_id=sf_id,
            sobject=sobject, approval_gate_id=approval_gate_id,
            status="enrolled",
        )
        enrolled.append({"lead_id": lead_id, "sf_record_id": sf_id})

    return {
        "enrolled": len(enrolled),
        "failed": failed,
        "rate_limit_hit": rate_hit,
        "sf_object": sobject,
        "approval_gate_id": approval_gate_id,
        "sequence_id": sequence_id,
    }


def _record_enrollment(
    *,
    lead_id: str,
    sequence_id: str,
    sf_record_id: str | None,
    sobject: str,
    approval_gate_id: int,
    status: str,
    error: str | None = None,
) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_sequence_enrollments
                    (lead_id, sequence_id, sf_record_id, sf_object,
                     approval_gate_id, status, error_message, enrolled_at)
                   VALUES (:lid, :sid, :sfid, :obj, :gate, :st, :err, :ts)"""
            ),
            {
                "lid": lead_id, "sid": sequence_id, "sfid": sf_record_id,
                "obj": sobject, "gate": approval_gate_id,
                "st": status, "err": error, "ts": datetime.now(timezone.utc),
            },
        )


# ----------------------------------------------- Slack-surface handlers


async def queue_status() -> dict[str, Any]:
    """List pending outbound_sequence approval gates. Shown via `@oo tof queue status`."""
    from shared.db.connection import get_engine
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT id, requested_at, justification, status
                   FROM approval_gates
                   WHERE action_type = :a AND status = 'pending'
                   ORDER BY id DESC LIMIT 20"""
            ),
            {"a": _GATE_ACTION},
        ).fetchall()

    if not rows:
        return {"text": "_No pending outbound_sequence gates._"}

    lines = ["*Pending outbound-sequence approval gates:*"]
    for r in rows:
        lines.append(f"• gate `{r[0]}` — requested {r[1]} — {r[2] or '(no justification)'}")
    lines.append(
        "\n_Approve any of these via_ `@oo tof queue approve <gate_id>` _or the Slack button._"
    )
    return {"text": "\n".join(lines)}


async def approve_queue(gate_id: int, *, approver: str = "o_via_oo") -> dict[str, Any]:
    """Approve an outbound_sequence gate via @oo mention (short-circuit for the
    Slack button). Refuses gates of other action_types.

    Responds with the approve outcome; caller should follow up with `enroll_batch`.
    """
    gate = get_approval_gate(gate_id)
    if gate is None:
        return {"text": f":warning: gate `{gate_id}` not found."}
    if gate["action_type"] != _GATE_ACTION:
        return {
            "text": (
                f":x: gate `{gate_id}` is `{gate['action_type']}` — "
                f"`{_GATE_ACTION}` only via this path. Use the normal Slack button."
            ),
        }
    if gate["status"] != "pending":
        return {"text": f":information_source: gate `{gate_id}` is `{gate['status']}`, not pending."}

    decide_approval_gate(gate_id, approved=True, approver=approver)
    return {"text": f":white_check_mark: Approved outbound_sequence gate `{gate_id}`."}
