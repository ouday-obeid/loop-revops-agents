"""D8 tests for sequence_enroller — 50/day rate-limit DoD, gate enforcement,
per-lead SF writes, enrollment audit, queue Slack surface."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel import sequence_enroller
from agents.top_of_funnel.state import get_state_engine
from shared.db.connection import get_engine as get_gov_engine
from shared.governance import (
    ApprovalRequired,
    RateLimitExceeded,
    create_approval_gate,
    decide_approval_gate,
)


@pytest.fixture(autouse=True)
def _reset_tables():
    sequence_enroller._ensure_enrollments_table()
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tof_sequence_enrollments"))
    gov = get_gov_engine()
    with gov.begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE bucket = :b"), {"b": "nooks_sequences_daily"})
        conn.execute(text("DELETE FROM approval_gates WHERE agent_name = 'top_of_funnel'"))
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'top_of_funnel'"))
    yield


# -------------------------------------------------- helpers


def _approved_gate() -> int:
    gid = create_approval_gate(
        agent_name="top_of_funnel",
        action_type="outbound_sequence",
        payload={"cadence": "q2-kickoff"},
        justification="Q2 kickoff blast",
    )
    decide_approval_gate(gid, approved=True, approver="o")
    return gid


def _make_create_fn():
    """Returns (create_fn, calls) — calls grows each invocation."""
    calls: list[dict[str, Any]] = []

    def fn(sobject: str, fields: dict[str, Any], **kw: Any) -> dict[str, Any]:
        calls.append({"sobject": sobject, "fields": dict(fields), **kw})
        return {"id": f"SF_{len(calls):03d}"}

    return fn, calls


# ==================================================== gate enforcement


def test_enroll_rejects_without_gate():
    fn, _ = _make_create_fn()
    with pytest.raises(ApprovalRequired):
        sequence_enroller.enroll_batch(["00Q001"], "0701", approval_gate_id=None, create_fn=fn)


def test_enroll_rejects_unapproved_gate():
    gid = create_approval_gate(
        agent_name="top_of_funnel",
        action_type="outbound_sequence",
        payload={},
        justification="pending",
    )
    fn, _ = _make_create_fn()
    with pytest.raises(ApprovalRequired):
        sequence_enroller.enroll_batch(["00Q001"], "0701", approval_gate_id=gid, create_fn=fn)


def test_enroll_rejects_wrong_action_type():
    gid = create_approval_gate(
        agent_name="top_of_funnel",
        action_type="bulk_update_small",
        payload={"count": 5},
        justification=None,
    )
    decide_approval_gate(gid, approved=True, approver="o")
    fn, _ = _make_create_fn()
    with pytest.raises(ApprovalRequired):
        sequence_enroller.enroll_batch(["00Q001"], "0701", approval_gate_id=gid, create_fn=fn)


# ==================================================== basic enrollment


def test_enroll_basic_happy_path():
    gid = _approved_gate()
    fn, calls = _make_create_fn()
    result = sequence_enroller.enroll_batch(
        ["00Q001", "00Q002", "00Q003"],
        "701ABC",
        approval_gate_id=gid,
        create_fn=fn,
    )
    assert result["enrolled"] == 3
    assert result["failed"] == []
    assert result["rate_limit_hit"] is False
    assert len(calls) == 3
    # CampaignMember is the default NOOKS_CADENCE_SF_OBJECT — CampaignId required.
    assert result["sf_object"] == "CampaignMember"
    for c in calls:
        assert c["fields"]["CampaignId"] == "701ABC"
        assert c["fields"]["LeadId"].startswith("00Q")

    # Audit rows persisted.
    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT lead_id, sf_record_id, status FROM tof_sequence_enrollments ORDER BY id")
        ).fetchall()
    assert len(rows) == 3
    assert all(r[2] == "enrolled" for r in rows)
    assert {r[1] for r in rows} == {"SF_001", "SF_002", "SF_003"}


def test_enroll_records_gate_id_in_audit():
    gid = _approved_gate()
    fn, _ = _make_create_fn()
    sequence_enroller.enroll_batch(["00Q001"], "701ABC", approval_gate_id=gid, create_fn=fn)

    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT approval_gate_id FROM tof_sequence_enrollments WHERE lead_id = '00Q001'")
        ).fetchone()
    assert row[0] == gid


# ==================================================== DoD: 51st blocked


def test_51st_enrollment_raises_rate_limit_and_no_write():
    """Rate limit DoD: 50 succeed, 51st raises RateLimitExceeded, and the SF
    create_fn is NOT called for the 51st lead (atomic pre-check)."""
    gid = _approved_gate()
    fn, calls = _make_create_fn()

    # First 50 in one batch — happy.
    lead_ids_50 = [f"00Q{i:03d}" for i in range(50)]
    r1 = sequence_enroller.enroll_batch(lead_ids_50, "701ABC", approval_gate_id=gid, create_fn=fn)
    assert r1["enrolled"] == 50
    assert r1["rate_limit_hit"] is False
    assert len(calls) == 50

    # 51st — the batch includes 1 lead; rate_limit_hit=True and create_fn NOT called.
    r2 = sequence_enroller.enroll_batch(["00Q051"], "701ABC", approval_gate_id=gid, create_fn=fn)
    assert r2["enrolled"] == 0
    assert r2["rate_limit_hit"] is True
    assert len(calls) == 50  # no additional SF calls

    # The attempt is audited with status='rate_limited'.
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT lead_id, status, error_message FROM tof_sequence_enrollments "
                "WHERE lead_id = '00Q051'"
            )
        ).fetchone()
    assert row is not None
    assert row[1] == "rate_limited"
    assert "nooks_sequences_daily" in (row[2] or "")


def test_rate_limit_stops_mid_batch():
    """A batch that straddles the cap: first N writes, then stops at N+1."""
    gid = _approved_gate()
    fn, calls = _make_create_fn()

    # Fill to 48 first.
    lead_ids_48 = [f"00Q{i:03d}" for i in range(48)]
    sequence_enroller.enroll_batch(lead_ids_48, "701ABC", approval_gate_id=gid, create_fn=fn)

    # Attempt 5 more — only 2 should succeed (bringing total to 50).
    lead_ids_5 = [f"00Q{i:03d}" for i in range(48, 53)]
    result = sequence_enroller.enroll_batch(lead_ids_5, "701ABC", approval_gate_id=gid, create_fn=fn)
    assert result["enrolled"] == 2
    assert result["rate_limit_hit"] is True
    assert len(calls) == 50


# ==================================================== create failure


def test_create_failure_logged_not_raised():
    """SF write error for one lead is recorded and the batch continues for
    the next (rate-limit check still consumes the slot for a failed lead)."""
    gid = _approved_gate()

    def flaky(sobject: str, fields: dict[str, Any], **kw: Any) -> dict[str, Any]:
        if fields["LeadId"] == "00Q_bad":
            raise RuntimeError("simulated SF 500")
        return {"id": f"SF_{fields['LeadId']}"}

    result = sequence_enroller.enroll_batch(
        ["00Q001", "00Q_bad", "00Q003"],
        "701ABC",
        approval_gate_id=gid,
        create_fn=flaky,
    )
    assert result["enrolled"] == 2
    assert len(result["failed"]) == 1
    assert result["failed"][0]["lead_id"] == "00Q_bad"
    assert "500" in result["failed"][0]["error"]

    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status, error_message FROM tof_sequence_enrollments WHERE lead_id = '00Q_bad'")
        ).fetchone()
    assert row[0] == "failed"
    assert "500" in row[1]


# ==================================================== SF object override


def test_non_campaignmember_object_omits_campaignid(monkeypatch):
    """If NOOKS_CADENCE_SF_OBJECT is a custom object (not CampaignMember),
    CampaignId should NOT be included in the SF create fields."""
    monkeypatch.setattr(sequence_enroller, "_cadence_sobject", lambda: "Nooks_Enrollment__c")
    gid = _approved_gate()
    fn, calls = _make_create_fn()
    sequence_enroller.enroll_batch(["00Q001"], "seq-9", approval_gate_id=gid, create_fn=fn)
    assert calls[0]["sobject"] == "Nooks_Enrollment__c"
    assert "CampaignId" not in calls[0]["fields"]
    assert calls[0]["fields"]["SequenceId__c"] == "seq-9"


# ==================================================== queue_status Slack


@pytest.mark.asyncio
async def test_queue_status_empty():
    r = await sequence_enroller.queue_status()
    assert "No pending" in r["text"]


@pytest.mark.asyncio
async def test_queue_status_lists_pending():
    create_approval_gate(
        agent_name="top_of_funnel",
        action_type="outbound_sequence",
        payload={"cadence": "a"},
        justification="q2 kickoff",
    )
    create_approval_gate(
        agent_name="top_of_funnel",
        action_type="outbound_sequence",
        payload={"cadence": "b"},
        justification="follow-up",
    )
    r = await sequence_enroller.queue_status()
    assert "Pending" in r["text"]
    assert "q2 kickoff" in r["text"]
    assert "follow-up" in r["text"]


# ==================================================== approve_queue Slack


@pytest.mark.asyncio
async def test_approve_queue_happy_path():
    gid = create_approval_gate(
        agent_name="top_of_funnel",
        action_type="outbound_sequence",
        payload={"cadence": "q2"},
        justification="Q2 kickoff",
    )
    r = await sequence_enroller.approve_queue(gid)
    assert "Approved" in r["text"]
    assert f"`{gid}`" in r["text"]

    # Now the gate is in 'approved' status.
    from shared.governance import get_approval_gate
    assert get_approval_gate(gid)["status"] == "approved"


@pytest.mark.asyncio
async def test_approve_queue_missing_gate():
    r = await sequence_enroller.approve_queue(999999)
    assert "not found" in r["text"]


@pytest.mark.asyncio
async def test_approve_queue_rejects_wrong_action_type():
    gid = create_approval_gate(
        agent_name="top_of_funnel",
        action_type="bulk_update_small",
        payload={"count": 5},
        justification=None,
    )
    r = await sequence_enroller.approve_queue(gid)
    assert "bulk_update_small" in r["text"]
    # Not approved.
    from shared.governance import get_approval_gate
    assert get_approval_gate(gid)["status"] == "pending"


@pytest.mark.asyncio
async def test_approve_queue_refuses_already_approved():
    gid = _approved_gate()  # already approved
    r = await sequence_enroller.approve_queue(gid)
    assert "approved" in r["text"]  # says it's already approved
