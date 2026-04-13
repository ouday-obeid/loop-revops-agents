"""Two-gate cooldown poller for `sf_schema_delete`.

Every 15 minutes: look for `approval_gates.status='approved_primary'` rows
whose `cooldown_until` has elapsed AND which don't already have a child
confirmation gate. For each, create a `sf_schema_delete_confirm` gate with
`parent_gate_id` set, and post a Slack approval message so O can finalize.

If O doesn't respond within the confirmation TTL (24h), the child gate
expires silently; the parent `approved_primary` row remains as audit
history. O can restart by re-issuing the delete command.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.governance import APPROVAL_TIERS, create_approval_gate

log = logging.getLogger(__name__)

CONFIRMATION_ACTION = "sf_schema_delete_confirm"
CONFIRMATION_TTL_HOURS = 24


def _find_ready_primaries(now: datetime) -> list[dict[str, Any]]:
    """Rows where cooldown_until ≤ now, no child confirmation gate exists yet."""
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT g.id, g.agent_name, g.action_type, g.payload, g.justification,
                       g.requested_by, g.cooldown_until
                  FROM approval_gates g
                 WHERE g.status = 'approved_primary'
                   AND g.cooldown_until IS NOT NULL
                   AND g.cooldown_until <= :now
                   AND NOT EXISTS (
                       SELECT 1 FROM approval_gates c
                        WHERE c.parent_gate_id = g.id
                         AND c.action_type = :child
                   )
                 ORDER BY g.id
                """
            ),
            {"now": now, "child": CONFIRMATION_ACTION},
        ).mappings().all()
    return [dict(r) for r in rows]


def _set_parent(child_id: int, parent_id: int) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE approval_gates SET parent_gate_id = :p WHERE id = :c"),
            {"p": parent_id, "c": child_id},
        )


def _post_confirmation_slack(
    child_id: int,
    parent: dict[str, Any],
    slack_sender_cls=None,
) -> str | None:
    """Post a Slack approval message for the child gate. Returns message ts.

    SlackSender is imported at call-time so tests can monkeypatch it.
    """
    if slack_sender_cls is None:
        from shared.slack_dispatcher import SlackSender as slack_sender_cls  # noqa: N813

    payload: dict[str, Any] = {}
    try:
        payload = json.loads(parent.get("payload") or "{}")
    except json.JSONDecodeError:
        payload = {"raw": parent.get("payload")}

    target = payload.get("target") or payload.get("object") or "<unspecified>"
    reason = parent.get("justification") or "(no justification attached)"
    text_body = (
        f":warning: *SF schema delete ready for final confirmation* (gate #{child_id})\n"
        f"Primary gate #{parent['id']} was approved after a {CONFIRMATION_TTL_HOURS}h cooling "
        f"period. Target: `{target}`\nReason: {reason}\n"
        f"This will delete against the prod write org."
    )
    sender = slack_sender_cls()
    return sender.send(channel="oo-dm", text_=text_body)


def poll() -> list[dict[str, Any]]:
    """Elevate ready primaries to confirmation gates. Returns per-gate info."""
    now = datetime.now(timezone.utc)
    tier = APPROVAL_TIERS.get(CONFIRMATION_ACTION)
    if tier is None:
        raise RuntimeError(
            f"APPROVAL_TIERS missing '{CONFIRMATION_ACTION}'; governance.py out of sync"
        )

    primaries = _find_ready_primaries(now)
    results: list[dict[str, Any]] = []
    for parent in primaries:
        payload = {}
        try:
            payload = json.loads(parent.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {"raw": parent.get("payload")}

        child_id = create_approval_gate(
            agent_name=parent["agent_name"],
            action_type=CONFIRMATION_ACTION,
            payload={**payload, "parent_gate_id": parent["id"]},
            justification=parent.get("justification"),
            requested_by="cooldown_poller",
            ttl_hours=CONFIRMATION_TTL_HOURS,
        )
        _set_parent(child_id, parent["id"])

        ts: str | None = None
        try:
            ts = _post_confirmation_slack(child_id, parent)
        except Exception as e:  # noqa: BLE001
            log.error("failed to post confirmation Slack for gate %s: %s", child_id, e)

        log.info(
            "elevated primary gate %s → confirmation gate %s (slack_ts=%s)",
            parent["id"], child_id, ts,
        )
        results.append({"parent_id": parent["id"], "child_id": child_id, "slack_ts": ts})

    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = poll()
    print(f"{len(out)} gate(s) elevated")
    for row in out:
        print(row)
