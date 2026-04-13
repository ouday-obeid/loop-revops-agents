"""M9 — CS approval gate state machine tests."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from agents.cs.handlers import slack_actions
from shared.db.connection import get_engine
from shared.governance import (
    ApprovalRequired,
    decide_approval_gate,
    get_approval_gate,
)


class FakeSf:
    def __init__(self):
        self.writes: list[tuple] = []

    def update_record(self, sobject, record_id, fields, **kw):
        self.writes.append((sobject, record_id, fields, kw))
        return {"id": record_id, "success": True}


class FakeSlack:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, channel, text_, blocks=None):
        self.sent.append((channel, text_))
        return {"ok": True, "ts": "0", "channel": channel}


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM audit_log"))
        # Null out self-FK first so cascades don't trip.
        conn.execute(text("UPDATE approval_gates SET parent_gate_id = NULL"))
        conn.execute(text("DELETE FROM approval_gates"))


@pytest.fixture(autouse=True)
def _clean():
    _clear(); yield; _clear()


# --- CSM reassignment --------------------------------------------------------

def test_csm_reassignment_round_trip():
    slack = FakeSlack()
    gate_id = slack_actions.request_csm_reassignment(
        account_id="001A", old_owner_id="005_old", new_owner_id="005_new",
        reason="territory change", slack_sender=slack,
    )
    assert slack.sent and "csm reassignment" in slack.sent[0][1].lower()

    # Simulate Jackie clicking approve.
    decide_approval_gate(gate_id, approved=True, approver="U_JACKIE")

    sf = FakeSf()
    result = slack_actions.finalize_csm_reassignment(gate_id, "U_JACKIE", sf_mcp=sf)
    assert result["success"] is True
    assert sf.writes[0][0] == "Account"
    assert sf.writes[0][2] == {"OwnerId": "005_new"}


def test_csm_reassignment_refuses_when_not_approved():
    gate_id = slack_actions.request_csm_reassignment(
        account_id="001A", old_owner_id=None, new_owner_id="005_new",
        reason="r",
    )
    # not decided — still pending
    with pytest.raises(ApprovalRequired):
        slack_actions.finalize_csm_reassignment(gate_id, "U_JACKIE", sf_mcp=FakeSf())


# --- Churn outreach ----------------------------------------------------------

def test_churn_outreach_posts_to_csm_after_approval():
    slack = FakeSlack()
    gate_id = slack_actions.request_churn_outreach(
        account_id="001A", csm_slack_id="U_CSM", draft_markdown="Hey — lets talk.",
        reason="tier 85", slack_sender=slack,
    )
    slack.sent.clear()
    decide_approval_gate(gate_id, approved=True, approver="U_JACKIE")

    result = slack_actions.finalize_churn_outreach(
        gate_id, "U_JACKIE", slack_sender=slack,
    )
    assert result["posted_to"] == "U_CSM"
    assert slack.sent and "lets talk" in slack.sent[0][1].lower()


# --- Mark Churned dual-approval ---------------------------------------------

def test_mark_churned_requires_both_gates():
    slack = FakeSlack()
    a_id = slack_actions.request_mark_churned(
        account_id="001A", justification="6 months no usage, contract expired",
        slack_sender=slack,
    )
    # Can't finalize without Gate B even if A is approved.
    decide_approval_gate(a_id, approved=True, approver="U_JACKIE")

    slack.sent.clear()
    b_id = slack_actions.on_mark_churned_primary_approved(a_id, slack_sender=slack)
    gate_b = get_approval_gate(b_id)
    assert gate_b["action_type"] == "mark_churned_confirm"
    assert gate_b["parent_gate_id"] == a_id
    assert slack.sent  # O pinged

    # Finalize B while still pending → refused.
    with pytest.raises(ApprovalRequired):
        slack_actions.finalize_mark_churned(b_id, "U_O", sf_mcp=FakeSf())

    # Approve B and execute.
    decide_approval_gate(b_id, approved=True, approver="U_O")
    sf = FakeSf()
    slack_actions.finalize_mark_churned(b_id, "U_O", sf_mcp=sf)
    assert sf.writes[0][0] == "Account"
    assert sf.writes[0][2] == {"Churn_Status__c": "Churned"}


def test_mark_churned_primary_must_be_approved_before_spawning_b():
    a_id = slack_actions.request_mark_churned(
        account_id="001A", justification="r",
    )
    # A still pending.
    with pytest.raises(ApprovalRequired):
        slack_actions.on_mark_churned_primary_approved(a_id)


def test_mark_churned_idempotent_b_creation():
    a_id = slack_actions.request_mark_churned(
        account_id="001A", justification="r",
    )
    decide_approval_gate(a_id, approved=True, approver="U_JACKIE")
    b1 = slack_actions.on_mark_churned_primary_approved(a_id)
    b2 = slack_actions.on_mark_churned_primary_approved(a_id)
    assert b1 == b2


def test_mark_churned_rejects_mismatched_parent_account():
    """Tampered payload between A and B is caught."""
    a_id = slack_actions.request_mark_churned(
        account_id="001A", justification="r",
    )
    decide_approval_gate(a_id, approved=True, approver="U_JACKIE")
    b_id = slack_actions.on_mark_churned_primary_approved(a_id)

    # Tamper: overwrite B's payload account_id to a different account.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE approval_gates SET payload = :p WHERE id = :id"""
            ),
            {
                "p": '{"account_id": "001B", "justification": "r", '
                     f'"parent_gate_id": {a_id}' + '}',
                "id": b_id,
            },
        )
    decide_approval_gate(b_id, approved=True, approver="U_O")
    with pytest.raises(ApprovalRequired):
        slack_actions.finalize_mark_churned(b_id, "U_O", sf_mcp=FakeSf())


def test_mark_churned_rejects_if_parent_rolled_back():
    """If A is somehow reverted to non-approved between B-create and B-finalize."""
    a_id = slack_actions.request_mark_churned(
        account_id="001A", justification="r",
    )
    decide_approval_gate(a_id, approved=True, approver="U_JACKIE")
    b_id = slack_actions.on_mark_churned_primary_approved(a_id)
    decide_approval_gate(b_id, approved=True, approver="U_O")

    # Simulate A being rejected out-of-band.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE approval_gates SET status = 'rejected' WHERE id = :id"),
            {"id": a_id},
        )

    with pytest.raises(ApprovalRequired):
        slack_actions.finalize_mark_churned(b_id, "U_O", sf_mcp=FakeSf())
