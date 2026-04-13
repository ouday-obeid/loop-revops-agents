"""CSM enforcer — null OwnerId create path + sweep + reassignment apply."""
from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from agents.onboarding import csm_enforcer
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _silence_slack(monkeypatch):
    class Silent:
        def send(self, *a, **kw):
            return {"ok": True}
    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())


def test_handle_created_with_owner_is_noop(make_opp):
    result = csm_enforcer.handle_created(make_opp(owner_id="005CSM"), "a01ABC")
    assert result is None


def test_handle_created_without_owner_creates_gate(make_opp):
    result = csm_enforcer.handle_created(make_opp(owner_id=None), "a01DEF")
    assert result is not None and result["posted"] is True
    gate_id = result["gate_id"]

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql_text("SELECT action_type, status, agent_name FROM approval_gates "
                     "WHERE id = :id"),
            {"id": gate_id},
        ).fetchone()
    assert row is not None
    assert row[0] == "csm_reassignment"
    assert row[1] == "pending"
    assert row[2] == "onboarding"


@pytest.mark.asyncio
async def test_sweep_posts_gate_for_each_unassigned(fake_sf_monkeypatch):
    fake_sf_monkeypatch.queue_soql({
        "records": [
            {"Id": "a01S1", "Name": "S1 Onboarding",
             "Account__r": {"Name": "Acme"}, "OwnerId": None,
             "CSM_2__c": None, "Opportunity__c": "006S1"},
            {"Id": "a01S2", "Name": "S2 Onboarding",
             "Account__r": {"Name": "Beta"}, "OwnerId": None,
             "CSM_2__c": "005CSM2", "Opportunity__c": "006S2"},
        ],
        "totalSize": 2, "done": True,
    })
    result = await csm_enforcer.sweep()
    assert result["unassigned"] == 2
    assert result["posted"] == 2


def test_apply_reassignment_writes_to_sf(fake_sf_monkeypatch, seed_gate):
    gate_id = seed_gate(
        action_type="csm_reassignment",
        status="approved",
        payload={"onboarding_id": "a01TO_REASSIGN"},
    )
    result = csm_enforcer.apply_reassignment(
        gate_id, new_owner_id="005NEW", approver="U_JACKIE"
    )
    assert result["success"] is True
    assert len(fake_sf_monkeypatch.updated) == 1
    updated = fake_sf_monkeypatch.updated[0]
    assert updated["sobject"] == "Onboarding__c"
    assert updated["id"] == "a01TO_REASSIGN"
    assert updated["fields"]["OwnerId"] == "005NEW"


def test_apply_reassignment_requires_approved_gate(fake_sf_monkeypatch, seed_gate):
    gate_id = seed_gate(
        action_type="csm_reassignment",
        status="pending",
        payload={"onboarding_id": "a01X"},
    )
    from shared.governance import ApprovalRequired
    with pytest.raises(ApprovalRequired):
        csm_enforcer.apply_reassignment(
            gate_id, new_owner_id="005NEW", approver="U_J"
        )


def test_apply_reassignment_rejects_bad_action_type(seed_gate):
    gate_id = seed_gate(action_type="single_record_update", status="approved",
                        payload={"onboarding_id": "a01X"})
    from shared.governance import ApprovalRequired
    with pytest.raises(ApprovalRequired):
        csm_enforcer.apply_reassignment(gate_id, new_owner_id="005NEW",
                                        approver="U_J")
