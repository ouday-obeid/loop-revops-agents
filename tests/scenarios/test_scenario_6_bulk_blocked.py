"""Scenario 6 — Bulk action blocked at 100+ records (tier `bulk_update_large`).

Monday item: 11736864051
Path: RevOps Support bulk_updater receives a 100+ row update request → governance
`classify_bulk_update` returns `bulk_update_large` → `require_approved_gate`
rejects if no gate, or if a smaller-tier gate is passed, or if the gate is
pending.

Validated boundaries:

  1. 100-row update with no gate raises ApprovalRequired before any SF call.
  2. A `bulk_update_small` (2-99) gate cannot be smuggled in for a 100-row
     update — require_approved_gate enforces action_type match.
  3. A `bulk_update_large` gate that is still `pending` is also rejected.
  4. A 99-row update classifies as `bulk_update_small` and requires the
     smaller-tier gate — boundary-on-boundary edge.

This is a governance-boundary test, not an end-to-end SF write. We never reach
the composite API; `require_approved_gate` is the gate we care about.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from agents.revops_support.data_quality import bulk_updater
from shared.db.connection import get_engine
from shared.governance import (
    ApprovalRequired,
    APPROVAL_TIERS,
    classify_bulk_update,
    create_approval_gate,
    decide_approval_gate,
)


@pytest.fixture(autouse=True)
def _clean():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM approval_gates"))
        conn.execute(text("DELETE FROM rate_limits"))
        conn.execute(text("DELETE FROM audit_log"))
    yield


def _updates(n: int) -> list[dict]:
    return [
        {"Id": f"001{i:018d}AAA"[:18], "Churn_Risk_Tier__c": "High"}
        for i in range(n)
    ]


def test_classifier_matches_phase1_tier_table():
    """Guardrail: governance table still maps 100+ rows to bulk_update_large."""
    assert classify_bulk_update(1) == "single_record_update"
    assert classify_bulk_update(2) == "bulk_update_small"
    assert classify_bulk_update(99) == "bulk_update_small"
    assert classify_bulk_update(100) == "bulk_update_large"
    assert classify_bulk_update(500) == "bulk_update_large"

    tier = APPROVAL_TIERS["bulk_update_large"]
    assert tier.gate == "slack_explicit"
    assert tier.approver == "o_only"
    assert tier.requires_justification is True


def test_bulk_update_100_rows_blocked_without_gate():
    with pytest.raises(ApprovalRequired):
        bulk_updater.bulk_update(
            "Account",
            _updates(100),
            agent_name="revops_support",
            approval_gate_id=None,  # type: ignore[arg-type]
        )


def test_bulk_update_100_rows_rejects_small_tier_gate():
    """A bulk_update_small gate cannot be swapped in for a 100-row batch."""
    gate_id = create_approval_gate(
        agent_name="revops_support",
        action_type="bulk_update_small",
        payload={"rows": 100, "why": "mislabeled"},
        justification="trying to sneak through",
    )
    decide_approval_gate(gate_id, approved=True, approver="user:ouday")

    with pytest.raises(ApprovalRequired, match="bulk_update_small"):
        bulk_updater.bulk_update(
            "Account",
            _updates(100),
            agent_name="revops_support",
            approval_gate_id=gate_id,
        )


def test_bulk_update_100_rows_rejects_pending_large_gate():
    """A bulk_update_large gate must be `approved`, not `pending`."""
    gate_id = create_approval_gate(
        agent_name="revops_support",
        action_type="bulk_update_large",
        payload={"rows": 100},
        justification="mass churn tier backfill",
    )
    # Do NOT decide — gate stays pending.

    with pytest.raises(ApprovalRequired, match="pending"):
        bulk_updater.bulk_update(
            "Account",
            _updates(100),
            agent_name="revops_support",
            approval_gate_id=gate_id,
        )


def test_bulk_update_99_rows_requires_small_tier_gate():
    """Boundary: 99 rows maps to bulk_update_small; missing gate blocks."""
    with pytest.raises(ApprovalRequired):
        bulk_updater.bulk_update(
            "Account",
            _updates(99),
            agent_name="revops_support",
            approval_gate_id=None,  # type: ignore[arg-type]
        )
