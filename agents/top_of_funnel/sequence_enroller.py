"""Sequence Enroller — writes to the SF object that Nooks mirrors.

Nooks is a read-only mirror of Salesforce, so "sequence enrollment" means
creating records in the SF object Nooks watches for cadence membership (exact
object name — CampaignMember / Task / Nooks custom object — confirmed with O
at D1 kickoff and documented in RUNBOOK.md).

Rate limit: 50/day via shared.governance.check_rate_limit("nooks_sequences_daily").
51st call raises RateLimitExceeded and writes an audit log entry.

8 AM review gate: request_approval("outbound_sequence", ...) fires at 07:55
during daily_briefing; this module refuses enrollment until today's gate is
approved by O.

Ships D8.
"""
from __future__ import annotations

from typing import Any


async def queue_status() -> dict[str, Any]:
    """Pending outbound-sequence approval gates. Implemented D8."""
    return {"text": "`queue status` not yet wired (ships D8)."}


async def approve_queue(gate_id: int) -> dict[str, Any]:
    """Approve today's enrollment queue by gate_id. Implemented D8."""
    return {"text": f"`queue approve` not yet wired (ships D8). gate_id={gate_id}"}
