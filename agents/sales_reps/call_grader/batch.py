"""Batch grader — date-range iteration over historical Fireflies transcripts.

Flow:
  1. fireflies_adapter.list_recent(from_date, to_date) → summary rows
  2. For each row: skip if already graded (storage.grade_exists) — idempotent replay
  3. Rate-limit via governance (bucket: sales_reps_grader_hourly, 100/hr)
  4. grader.grade_one(meeting_id) — per-row, failures isolated so one bad call doesn't stop the run
  5. Aggregate counts and return a Slack-renderable summary

Used by the `@oo sales-reps batch-grade <from> <to>` subcommand and by the
Fireflies-poll scheduled job (every 15 min, with a short look-back window).
"""
from __future__ import annotations

import logging
from typing import Any

from agents.sales_reps import rate_gates
from agents.sales_reps.call_grader import fireflies_adapter, grader, storage

log = logging.getLogger(__name__)

_RATE_BUCKET = "sales_reps_grader_hourly"


async def grade_range(
    from_date: str,
    to_date: str,
    *,
    limit: int = 100,
    skip_already_graded: bool = True,
    allow_haiku: bool = True,
) -> dict[str, Any]:
    """Grade every gradable call in [from_date, to_date]. Returns summary."""
    rows = fireflies_adapter.list_recent(from_date=from_date, to_date=to_date, limit=limit)
    graded: list[dict[str, Any]] = []
    skipped_non_gradable: list[dict[str, Any]] = []
    skipped_already: list[str] = []
    errors: list[dict[str, str]] = []
    rate_limited_stopped_at: str | None = None

    for row in rows:
        meeting_id = row.get("id")
        if not meeting_id:
            continue

        if skip_already_graded and storage.grade_exists(meeting_id):
            skipped_already.append(meeting_id)
            continue

        try:
            # Hourly rate gate. Atomic increment; raises if we'd go over.
            # On hit, stop the run — future ticks will pick up where we left off.
            rate_gates.check(_RATE_BUCKET, window_seconds=3600)
        except rate_gates.RateGateExceeded as e:
            log.warning("batch grader stopped: %s", e)
            rate_limited_stopped_at = meeting_id
            break

        try:
            result = await grader.grade_one(meeting_id, allow_haiku=allow_haiku)
        except Exception as e:  # noqa: BLE001 — batch must isolate per-row failures
            log.exception("grade failed meeting=%s", meeting_id)
            errors.append({"meeting_id": meeting_id, "error": f"{type(e).__name__}: {e}"})
            continue

        if result.get("skipped"):
            skipped_non_gradable.append({
                "meeting_id": meeting_id,
                "call_type": result.get("call_type"),
            })
        else:
            graded.append({
                "meeting_id": meeting_id,
                "call_type": result.get("call_type"),
                "percentage": result.get("percentage"),
                "grade_label": result.get("grade_label"),
            })

    text_lines = [
        f"*Batch grade complete* — {from_date}..{to_date}",
        f"• Graded: {len(graded)}",
        f"• Non-gradable (skipped): {len(skipped_non_gradable)}",
        f"• Already graded (skipped): {len(skipped_already)}",
        f"• Errors: {len(errors)}",
    ]
    if rate_limited_stopped_at:
        text_lines.append(f"• ⛔ stopped early — rate limit hit at {rate_limited_stopped_at}")

    return {
        "text": "\n".join(text_lines),
        "from": from_date,
        "to": to_date,
        "graded": graded,
        "skipped_non_gradable": skipped_non_gradable,
        "skipped_already_graded": skipped_already,
        "errors": errors,
        "rate_limited_stopped_at": rate_limited_stopped_at,
    }
