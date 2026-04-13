"""Verify the onboarding-specific tiers and auto_approve_gate helper."""
from __future__ import annotations

import pytest

from shared.governance import (
    APPROVAL_TIERS,
    ApprovalRequired,
    auto_approve_gate,
    create_approval_gate,
    get_approval_gate,
)


def test_onboarding_auto_create_tier_is_auto_approve():
    tier = APPROVAL_TIERS["onboarding_auto_create"]
    assert tier.gate == "auto_approve"
    assert tier.approver == "system"


def test_csm_reassignment_tier_routes_to_jackie_or_o():
    tier = APPROVAL_TIERS["csm_reassignment"]
    assert tier.gate == "slack_button"
    assert "jackie" in tier.approver.lower()


def test_skip_milestone_requires_justification():
    tier = APPROVAL_TIERS["skip_milestone"]
    assert tier.requires_justification is True


def test_auto_approve_gate_approves_onboarding_auto_create():
    gate_id = create_approval_gate(
        agent_name="onboarding",
        action_type="onboarding_auto_create",
        payload={"origin": "test"},
        justification=None,
    )
    auto_approve_gate(gate_id, approver="system:onboarding")
    gate = get_approval_gate(gate_id)
    assert gate["status"] == "approved"
    assert gate["approved_by"] == "system:onboarding"


def test_auto_approve_gate_refuses_non_auto_tier():
    gate_id = create_approval_gate(
        agent_name="onboarding",
        action_type="csm_reassignment",  # slack_button, not auto_approve
        payload={},
        justification=None,
    )
    with pytest.raises(ApprovalRequired, match="not an auto_approve tier"):
        auto_approve_gate(gate_id)


def test_auto_approve_gate_missing_gate_raises():
    with pytest.raises(ApprovalRequired, match="not found"):
        auto_approve_gate(999_999_999)
