"""Tests for permissions: user_provisioner, access_grant, offboarding, license_audit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text


# -------------------------------------------------------------- shared helpers


def _approved_gate(action_type: str) -> int:
    """Insert an already-approved gate so require_approved_gate() passes."""
    from shared.db.connection import get_engine
    now = datetime.now(timezone.utc)
    with get_engine().begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO approval_gates "
                "(agent_name, action_type, payload, justification, requested_by, "
                " status, requested_at, approved_by, decided_at) "
                "VALUES ('revops_support', :act, '{}', 'test', 'O', "
                " 'approved', :rq, 'O', :dc)"
            ),
            {"act": action_type, "rq": now, "dc": now},
        )
        gate_id = result.lastrowid
        if gate_id is None:
            gate_id = conn.execute(
                text("SELECT id FROM approval_gates ORDER BY id DESC LIMIT 1")
            ).fetchone()[0]
        return int(gate_id)


@pytest.fixture
def _clean_tables():
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'revops_support'"))
        conn.execute(text("DELETE FROM approval_gates WHERE agent_name = 'revops_support'"))
        conn.execute(text("DELETE FROM tasks WHERE agent_name = 'revops_support'"))
    yield
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM audit_log WHERE agent_name = 'revops_support'"))
        conn.execute(text("DELETE FROM approval_gates WHERE agent_name = 'revops_support'"))
        conn.execute(text("DELETE FROM tasks WHERE agent_name = 'revops_support'"))


class _FakeSF:
    """Minimal stand-in for shared.mcp.salesforce_mcp in these tests."""

    def __init__(self):
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.queries: list[str] = []
        self._next_id = 100
        # seed responses keyed by substring-of-query → records
        self.query_responses: dict[str, list[dict[str, Any]]] = {}

    def _alloc_id(self, prefix="0Px"):
        self._next_id += 1
        return f"{prefix}{self._next_id:015d}"

    def soql_query(self, q, limit=1):
        self.queries.append(q)
        for key, records in self.query_responses.items():
            if key in q:
                return {"records": records, "totalSize": len(records)}
        return {"records": [], "totalSize": 0}

    def create_record(self, sobject, fields, *, agent_name, approval_gate_id, intent="write"):
        new_id = self._alloc_id()
        self.created.append(
            {"sobject": sobject, "fields": fields, "gate": approval_gate_id, "id": new_id}
        )
        return {"id": new_id, "success": True}

    def update_record(self, sobject, record_id, fields, *, agent_name, approval_gate_id, intent="write"):
        self.updated.append(
            {"sobject": sobject, "id": record_id, "fields": fields, "gate": approval_gate_id}
        )
        return {"id": record_id, "success": True}


# -------------------------------------------------------------- user_provisioner


def test_provision_creates_user_and_assigns(_clean_tables):
    from agents.revops_support.permissions.user_provisioner import (
        ProvisionRequest, provision,
    )

    gate = _approved_gate("user_provisioning")
    fake = _FakeSF()
    req = ProvisionRequest(
        first_name="Jane", last_name="Doe",
        email="jane.doe@tryloop.ai", username="jane.doe@tryloop.ai",
        alias="jdoe", profile_id="00e000000000001AAA",
        role_id="00E000000000002AAA",
        permission_set_ids=["0PS001", "0PS002"],
        group_ids=["00G001"],
    )
    result = provision(req, approval_gate_id=gate, sf_mcp=fake)

    assert result.user_id
    assert len(result.permission_set_assignments) == 2
    assert len(result.group_memberships) == 1

    # User create carried all required fields.
    user_create = next(c for c in fake.created if c["sobject"] == "User")
    for f in ("FirstName", "LastName", "Email", "Username", "ProfileId",
              "TimeZoneSidKey", "LocaleSidKey"):
        assert user_create["fields"][f]


def test_provision_rejects_missing_required_fields(_clean_tables):
    from agents.revops_support.permissions.user_provisioner import (
        ProvisionRequest, provision,
    )

    gate = _approved_gate("user_provisioning")
    fake = _FakeSF()
    req = ProvisionRequest(
        first_name="", last_name="Doe",
        email="j@x", username="j@x", alias="j",
        profile_id="p",
    )
    with pytest.raises(ValueError):
        provision(req, approval_gate_id=gate, sf_mcp=fake)


# -------------------------------------------------------------- access_grant


def test_grant_permission_set_idempotent(_clean_tables):
    from agents.revops_support.permissions.access_grant import grant_permission_set

    gate = _approved_gate("permission_grant")
    fake = _FakeSF()
    fake.query_responses["FROM PermissionSetAssignment"] = [
        {"Id": "0Pa00000001"}
    ]
    result = grant_permission_set(
        "005U1", "0PSU1", approval_gate_id=gate, sf_mcp=fake,
    )
    assert result.was_existing is True
    assert fake.created == []  # no write when already granted


def test_grant_permission_set_creates_new(_clean_tables):
    from agents.revops_support.permissions.access_grant import grant_permission_set

    gate = _approved_gate("permission_grant")
    fake = _FakeSF()
    fake.query_responses["FROM PermissionSetAssignment"] = []
    result = grant_permission_set(
        "005U1", "0PSU1", approval_gate_id=gate, sf_mcp=fake,
    )
    assert result.was_existing is False
    assert len(fake.created) == 1
    assert fake.created[0]["sobject"] == "PermissionSetAssignment"


def test_revoke_permission_set_deletes_via_sf_cli(_clean_tables):
    from agents.revops_support.permissions.access_grant import revoke_permission_set

    gate = _approved_gate("permission_grant")
    fake = _FakeSF()
    fake.query_responses["FROM PermissionSetAssignment"] = [
        {"Id": "0Pa00000001"}
    ]
    deleted: list[tuple[str, str]] = []

    def fake_delete(sobject, record_id):
        deleted.append((sobject, record_id))

    revoke_permission_set(
        "005U1", "0PSU1",
        approval_gate_id=gate, sf_mcp=fake, sf_delete=fake_delete,
    )
    assert deleted == [("PermissionSetAssignment", "0Pa00000001")]


# -------------------------------------------------------------- offboarding


def test_offboarding_without_transfer_counts_only(_clean_tables):
    from agents.revops_support.permissions import offboarding

    gate = _approved_gate("license_deactivation")
    fake = _FakeSF()
    fake.query_responses["FROM Account"] = [{"expr0": 14}]
    fake.query_responses["FROM Opportunity"] = [{"expr0": 3}]
    fake.query_responses["FROM Lead"] = [{"expr0": 0}]
    fake.query_responses["FROM Case"] = [{"expr0": 1}]

    req = offboarding.OffboardRequest(user_id="005U1", transfer_to_user_id=None)
    result = offboarding.offboard(req, approval_gate_id=gate, sf_mcp=fake)

    assert result.deactivated is True
    assert result.reassigned == {"Account": 14, "Opportunity": 3, "Lead": 0, "Case": 1}
    # Deactivation update was issued.
    assert any(u["sobject"] == "User" and u["fields"] == {"IsActive": False}
               for u in fake.updated)
    # Task surfaced for package access revocation.
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM tasks WHERE source = "
                "'revops_support:offboarding:package_access:005U1'"
            )
        ).fetchone()
    assert row is not None


def test_offboarding_with_transfer_reassigns(_clean_tables):
    from agents.revops_support.permissions import offboarding

    gate = _approved_gate("license_deactivation")
    fake = _FakeSF()
    fake.query_responses["FROM Account"] = [
        {"Id": "001AAA"}, {"Id": "001BBB"}
    ]
    fake.query_responses["FROM Opportunity"] = []
    fake.query_responses["FROM Lead"] = []
    fake.query_responses["FROM Case"] = []

    @dataclass
    class FakeBulkModule:
        calls: list[dict[str, Any]]

        def bulk_update(self, sobject, updates, *, agent_name, approval_gate_id, dry_run=False):
            self.calls.append({"sobject": sobject, "updates": updates, "gate": approval_gate_id})
            return {"simulated": True}

    bulk = FakeBulkModule(calls=[])
    req = offboarding.OffboardRequest(user_id="005U1", transfer_to_user_id="005U2")
    result = offboarding.offboard(
        req, approval_gate_id=gate, sf_mcp=fake, bulk_updater=bulk,
    )
    assert result.reassigned["Account"] == 2
    # Bulk-update for Account called with the OwnerId reassignment.
    acct_calls = [c for c in bulk.calls if c["sobject"] == "Account"]
    assert len(acct_calls) == 1
    for u in acct_calls[0]["updates"]:
        assert u["OwnerId"] == "005U2"


# -------------------------------------------------------------- license_audit


def test_license_audit_surfaces_inactive_users(_clean_tables):
    from agents.revops_support.permissions import license_audit

    def fake_soql(q, limit=500):
        return {"records": [
            {"Id": "005A", "Username": "ghost@tryloop.ai", "Email": "g@x.com",
             "LastLoginDate": None, "Profile": {"Name": "Sales User"}},
            {"Id": "005B", "Username": "sync@tryloop.ai", "Email": "s@x.com",
             "LastLoginDate": None, "Profile": {"Name": "Integration User"}},
        ]}

    out = license_audit.run(soql_query=fake_soql, inactive_days=60)
    # Integration User filtered out; one task surfaced.
    assert len(out) == 1
    assert out[0].username == "ghost@tryloop.ai"

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        cnt = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source = 'revops_support:license_audit:005A'")
        ).fetchone()[0]
    assert cnt == 1


def test_license_audit_is_idempotent(_clean_tables):
    from agents.revops_support.permissions import license_audit

    def fake_soql(q, limit=500):
        return {"records": [
            {"Id": "005A", "Username": "ghost@tryloop.ai", "Email": None,
             "LastLoginDate": None, "Profile": {"Name": "Sales User"}},
        ]}

    license_audit.run(soql_query=fake_soql)
    license_audit.run(soql_query=fake_soql)

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        cnt = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source = 'revops_support:license_audit:005A'")
        ).fetchone()[0]
    assert cnt == 1
