"""Cover slack_dispatcher's pure-Python paths (no Bolt runtime)."""
import asyncio

from sqlalchemy import text

from shared import slack_dispatcher
from shared.db.connection import get_engine


def test_dispatch_no_handler():
    # Unregister oo if present to force the no-handler path
    slack_dispatcher._registry.pop("oo", None)
    result = asyncio.run(slack_dispatcher.dispatch("nothing here", {}))
    assert "error" in result


def test_dispatch_registered_handler():
    async def dummy(payload): return {"text": "hi"}
    slack_dispatcher.register("dummy_agent", dummy)
    result = asyncio.run(slack_dispatcher.dispatch("dummy_agent go", {}))
    assert result == {"text": "hi"}


def test_handle_gate_decision_updates_db():
    from shared import governance
    gid = governance.create_approval_gate(
        agent_name="t", action_type="single_record_update", payload={}, justification=None
    )
    slack_dispatcher.handle_gate_decision(gid, True, "USLACK")
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT status, approved_by FROM approval_gates WHERE id = :i"), {"i": gid}
        ).fetchone()
    assert row[0] == "approved"
    assert row[1] == "USLACK"


def test_render_various():
    assert slack_dispatcher._render("hello") == "hello"
    assert slack_dispatcher._render({"text": "hi"}) == "hi"
    out = slack_dispatcher._render({"a": 1})
    assert "```" in out


def test_parse_command_bare():
    agent, rest = slack_dispatcher.parse_command("<@U1> oo ping")
    assert agent is None
    assert "ping" in rest


def test_approval_blocks_content():
    blocks = slack_dispatcher.approval_blocks(7, "bulk_update_large", "summary text")
    assert blocks[0]["type"] == "header"
    assert "summary text" in str(blocks)


# ---------- Phase 1.5 FIX: cooldown bypass ----------

def test_handle_gate_decision_refuses_approve_when_parent_cooldown_not_elapsed():
    """Defense-in-depth: approving a confirm-child whose parent's cooldown
    has not elapsed must raise, regardless of whether the poller created it
    early by mistake.
    """
    from datetime import datetime, timedelta, timezone
    import pytest
    from sqlalchemy import text
    from shared import governance, slack_dispatcher
    from shared.db.connection import get_engine

    now = datetime.now(timezone.utc)
    future_cd = now + timedelta(hours=12)  # 12h still in the future
    with get_engine().begin() as conn:
        # Parent primary gate, approved_primary with cooldown in the future
        r = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at, cooldown_until) "
                "VALUES ('t', 'sf_schema_delete', '{}', 'delete', 'O', "
                " 'approved_primary', :n, 'O', :n, :cd)"
            ),
            {"n": now, "cd": future_cd},
        )
        parent_id = r.lastrowid or conn.execute(
            text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
        ).fetchone()[0]
        # Confirm child created (prematurely)
        r2 = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, parent_gate_id) "
                "VALUES ('t', 'sf_schema_delete_confirm', '{}', null, 'O', "
                " 'pending', :n, :pid)"
            ),
            {"n": now, "pid": parent_id},
        )
        child_id = r2.lastrowid or conn.execute(
            text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
        ).fetchone()[0]

    try:
        with pytest.raises(governance.ApprovalRequired, match="cooldown"):
            slack_dispatcher.handle_gate_decision(child_id, True, "O")
        # Confirm gate should still be pending — no status flip
        with get_engine().begin() as conn:
            status = conn.execute(
                text("SELECT status FROM approval_gates WHERE id = :id"),
                {"id": child_id},
            ).scalar_one()
        assert status == "pending"
    finally:
        with get_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM audit_log WHERE target LIKE 'gate_%'")
            )
            conn.execute(
                text("DELETE FROM approval_gates WHERE id IN (:c, :p)"),
                {"c": child_id, "p": parent_id},
            )


def test_handle_gate_decision_allows_approve_when_parent_cooldown_elapsed():
    """Mirror test: once cooldown_until is in the past, the confirm is allowed."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from shared import governance, slack_dispatcher
    from shared.db.connection import get_engine

    now = datetime.now(timezone.utc)
    past_cd = now - timedelta(hours=1)  # Already elapsed
    with get_engine().begin() as conn:
        r = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at, cooldown_until) "
                "VALUES ('t', 'sf_schema_delete', '{}', 'delete', 'O', "
                " 'approved_primary', :n, 'O', :n, :cd)"
            ),
            {"n": now, "cd": past_cd},
        )
        parent_id = r.lastrowid or conn.execute(
            text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
        ).fetchone()[0]
        r2 = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, parent_gate_id) "
                "VALUES ('t', 'sf_schema_delete_confirm', '{}', null, 'O', "
                " 'pending', :n, :pid)"
            ),
            {"n": now, "pid": parent_id},
        )
        child_id = r2.lastrowid or conn.execute(
            text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
        ).fetchone()[0]

    try:
        slack_dispatcher.handle_gate_decision(child_id, True, "O")
        with get_engine().begin() as conn:
            status = conn.execute(
                text("SELECT status FROM approval_gates WHERE id = :id"),
                {"id": child_id},
            ).scalar_one()
        assert status == "approved"
    finally:
        with get_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM audit_log WHERE target LIKE 'gate_%'")
            )
            conn.execute(
                text("DELETE FROM approval_gates WHERE id IN (:c, :p)"),
                {"c": child_id, "p": parent_id},
            )


def test_handle_gate_decision_rejects_pass_through_even_during_cooldown():
    """A reject click during cooldown still flows through — operators must
    always be able to withdraw approval regardless of cooldown state."""
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import text
    from shared import slack_dispatcher
    from shared.db.connection import get_engine

    now = datetime.now(timezone.utc)
    future_cd = now + timedelta(hours=12)
    with get_engine().begin() as conn:
        r = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at, cooldown_until) "
                "VALUES ('t', 'sf_schema_delete', '{}', 'delete', 'O', "
                " 'approved_primary', :n, 'O', :n, :cd)"
            ),
            {"n": now, "cd": future_cd},
        )
        parent_id = r.lastrowid or conn.execute(
            text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
        ).fetchone()[0]
        r2 = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, parent_gate_id) "
                "VALUES ('t', 'sf_schema_delete_confirm', '{}', null, 'O', "
                " 'pending', :n, :pid)"
            ),
            {"n": now, "pid": parent_id},
        )
        child_id = r2.lastrowid or conn.execute(
            text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
        ).fetchone()[0]

    try:
        # Reject should flow through even with cooldown unelapsed
        slack_dispatcher.handle_gate_decision(child_id, False, "O")
        with get_engine().begin() as conn:
            status = conn.execute(
                text("SELECT status FROM approval_gates WHERE id = :id"),
                {"id": child_id},
            ).scalar_one()
        assert status == "rejected"
    finally:
        with get_engine().begin() as conn:
            conn.execute(
                text("DELETE FROM audit_log WHERE target LIKE 'gate_%'")
            )
            conn.execute(
                text("DELETE FROM approval_gates WHERE id IN (:c, :p)"),
                {"c": child_id, "p": parent_id},
            )
