import pytest

from shared import governance
from shared.governance import ApprovalRequired, RateLimitExceeded


def test_classify_bulk_update():
    assert governance.classify_bulk_update(1) == "single_record_update"
    assert governance.classify_bulk_update(50) == "bulk_update_small"
    assert governance.classify_bulk_update(100) == "bulk_update_large"


def test_approval_gate_lifecycle():
    gid = governance.create_approval_gate(
        agent_name="test", action_type="single_record_update", payload={"x": 1}, justification=None
    )
    g = governance.get_approval_gate(gid)
    assert g["status"] == "pending"
    governance.decide_approval_gate(gid, approved=True, approver="UTEST")
    assert governance.get_approval_gate(gid)["status"] == "approved"


def test_bulk_large_requires_justification():
    with pytest.raises(ApprovalRequired):
        governance.create_approval_gate(
            agent_name="test", action_type="bulk_update_large", payload={"count": 500}, justification=None
        )


def test_require_approved_gate_rejects_unapproved():
    gid = governance.create_approval_gate(
        agent_name="test", action_type="single_record_update", payload={}, justification=None
    )
    with pytest.raises(ApprovalRequired):
        governance.require_approved_gate(gid, action_type="single_record_update")


def test_rate_limit_increments_and_trips():
    # nooks_sequences_daily limit is 50
    for _ in range(50):
        governance.check_rate_limit("nooks_sequences_daily")
    with pytest.raises(RateLimitExceeded):
        governance.check_rate_limit("nooks_sequences_daily")


def test_write_audit_persists():
    governance.write_audit(agent_name="test", action="test_action", target="sf:Test:001", after={"x": 1})
    from sqlalchemy import text
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        row = conn.execute(text("SELECT COUNT(*) FROM audit_log WHERE action='test_action'")).scalar()
    assert row >= 1


def test_schema_delete_sets_approved_primary_with_cooldown():
    gid = governance.create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete",
        payload={"field": "Opportunity.Legacy__c"},
        justification="unused since 2024",
    )
    governance.decide_approval_gate(gid, approved=True, approver="UTEST")
    gate = governance.get_approval_gate(gid)
    assert gate["status"] == "approved_primary"
    assert gate["cooldown_until"] is not None


def test_require_approved_gate_rejects_approved_primary():
    gid = governance.create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete",
        payload={"field": "Account.X__c"},
        justification="cleanup",
    )
    governance.decide_approval_gate(gid, approved=True, approver="UTEST")
    # approved_primary is NOT sufficient to execute — confirmation gate required
    with pytest.raises(ApprovalRequired):
        governance.require_approved_gate(gid, action_type="sf_schema_delete")


def test_schema_delete_confirm_approves_normally():
    gid = governance.create_approval_gate(
        agent_name="revops_support",
        action_type="sf_schema_delete_confirm",
        payload={"parent_gate_id": 999},
        justification=None,
    )
    governance.decide_approval_gate(gid, approved=True, approver="UTEST")
    gate = governance.get_approval_gate(gid)
    assert gate["status"] == "approved"
    assert gate["cooldown_until"] is None


def _clear_bucket(bucket: str) -> None:
    from sqlalchemy import text
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE bucket = :b"), {"b": bucket})


def test_soft_rate_limit_does_not_raise():
    # revops_schema_changes_weekly is in SOFT_LIMIT_BUCKETS, limit=10
    _clear_bucket("revops_schema_changes_weekly")
    for _ in range(10):
        governance.check_rate_limit("revops_schema_changes_weekly", window_seconds=604800)
    # 11th call would breach — must NOT raise
    count = governance.check_rate_limit(
        "revops_schema_changes_weekly", window_seconds=604800
    )
    assert count == 11


def test_hard_mode_override_raises_on_soft_bucket():
    _clear_bucket("revops_schema_changes_weekly")
    for _ in range(10):
        governance.check_rate_limit(
            "revops_schema_changes_weekly", window_seconds=604800, mode="hard"
        )
    with pytest.raises(RateLimitExceeded):
        governance.check_rate_limit(
            "revops_schema_changes_weekly", window_seconds=604800, mode="hard"
        )


def test_new_revops_tiers_addressable():
    for action in (
        "sf_schema_create",
        "sf_schema_modify",
        "sf_schema_delete",
        "sf_schema_delete_confirm",
        "user_provisioning",
        "permission_grant",
        "license_deactivation",
    ):
        assert action in governance.APPROVAL_TIERS
