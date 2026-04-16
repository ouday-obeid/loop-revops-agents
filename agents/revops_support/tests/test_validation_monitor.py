"""Tests for data_quality.validation_monitor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.revops_support.data_quality import validation_monitor as vm


@pytest.fixture
def _fresh_state():
    """Wipe tasks + audit_log rows this module writes so each test starts clean."""
    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "DELETE FROM audit_log WHERE agent_name = 'revops_support' "
                "AND action = 'validation_monitor_poll'"
            )
        )
        conn.execute(
            text(
                "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'validation_rule_review'"
            )
        )
    yield
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "DELETE FROM audit_log WHERE agent_name = 'revops_support' "
                "AND action = 'validation_monitor_poll'"
            )
        )
        conn.execute(
            text(
                "DELETE FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'validation_rule_review'"
            )
        )


def _rule(
    id_: str = "03d000000000001",
    name: str = "RequireCloseReason",
    obj: str = "Opportunity",
    formula: str = "ISBLANK(Close_Reason__c)",
    last_modified: str | None = None,
    owner: str = "Duncan McGillivray",
) -> dict[str, Any]:
    return {
        "Id": id_,
        "ValidationName": name,
        "Active": True,
        "ErrorMessage": "Close reason required",
        "Description": "Ensures close reason is captured before save",
        "ErrorConditionFormula": formula,
        "EntityDefinition": {"QualifiedApiName": obj},
        "LastModifiedDate": last_modified or datetime.now(timezone.utc).isoformat(),
        "LastModifiedBy": {"Name": owner},
    }


def _mk_tooling(records: list[dict[str, Any]]):
    def tq(_query: str, limit: int = 2000) -> dict[str, Any]:
        return {"records": records}
    return tq


def _mk_describe(fields_by_obj: dict[str, list[str]]):
    def describe(obj: str) -> dict[str, Any]:
        names = fields_by_obj.get(obj, [])
        return {"fields": [{"name": n} for n in names]}
    return describe


def test_fetch_active_rules_normalizes_shape():
    rules = vm.fetch_active_rules(tooling_query=_mk_tooling([_rule()]))
    assert len(rules) == 1
    assert rules[0]["object"] == "Opportunity"
    assert rules[0]["owner"] == "Duncan McGillivray"
    assert rules[0]["formula"] == "ISBLANK(Close_Reason__c)"


def test_referenced_custom_fields_extracts_c_suffix():
    assert vm._referenced_custom_fields("ISBLANK(Foo__c)") == {"Foo__c"}
    # Cross-object path returns the compound identifier; caller splits.
    assert "Account.Bar__c" in vm._referenced_custom_fields("NOT(Account.Bar__c)")
    assert vm._referenced_custom_fields("LEN(Name) > 0") == set()


def test_detect_orphans_flags_missing_custom_field():
    rules = [
        {
            "id": "1", "name": "R", "object": "Opportunity",
            "formula": "ISBLANK(Gone__c)", "last_modified": None, "owner": "X",
        }
    ]
    orphans = vm.detect_orphans(rules, describe_fn=_mk_describe({"Opportunity": ["Name"]}))
    assert len(orphans) == 1
    assert orphans[0]["issue"] == "orphaned_field_reference"
    assert orphans[0]["missing_fields"] == ["Gone__c"]


def test_detect_orphans_passes_when_field_exists():
    rules = [
        {
            "id": "1", "name": "R", "object": "Opportunity",
            "formula": "ISBLANK(Live__c)", "last_modified": None, "owner": "X",
        }
    ]
    orphans = vm.detect_orphans(
        rules, describe_fn=_mk_describe({"Opportunity": ["Live__c"]})
    )
    assert orphans == []


def test_detect_orphans_survives_describe_failure():
    def boom(_obj):
        raise RuntimeError("sf CLI died")

    rules = [
        {
            "id": "1", "name": "R", "object": "Opportunity",
            "formula": "ISBLANK(X__c)", "last_modified": None, "owner": "X",
        }
    ]
    orphans = vm.detect_orphans(rules, describe_fn=boom)
    # Describe crashes → we can't validate → rule is flagged as orphan
    # (missing set is {X__c} because the cached empty set has no X__c).
    assert len(orphans) == 1
    assert "X__c" in orphans[0]["missing_fields"]


def test_detect_stale_flags_old_rule():
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)
    old_ts = (now - timedelta(days=600)).isoformat()
    rules = [{"id": "1", "name": "R", "object": "Opportunity", "formula": "",
              "last_modified": old_ts, "owner": "X"}]
    stale = vm.detect_stale(rules, stale_days=540, now=now)
    assert len(stale) == 1
    assert stale[0]["age_days"] == 600


def test_detect_stale_respects_threshold():
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)
    recent_ts = (now - timedelta(days=100)).isoformat()
    rules = [{"id": "1", "name": "R", "object": "Opportunity", "formula": "",
              "last_modified": recent_ts, "owner": "X"}]
    assert vm.detect_stale(rules, stale_days=540, now=now) == []


def test_summarize_counts_by_object_and_owner():
    rules = [
        {"object": "Account", "owner": "Duncan"},
        {"object": "Account", "owner": "Duncan"},
        {"object": "Opportunity", "owner": "Ouday"},
    ]
    s = vm.summarize(rules)
    assert s["total"] == 3
    assert s["by_object"] == {"Account": 2, "Opportunity": 1}
    assert s["by_owner"] == {"Duncan": 2, "Ouday": 1}


def test_poll_creates_tasks_and_writes_audit(_fresh_state):
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)
    stale_ts = (now - timedelta(days=700)).isoformat()
    records = [
        # Orphan: formula references Missing__c which describe doesn't know
        _rule(id_="R1", name="OrphanRule", formula="ISBLANK(Missing__c)"),
        # Stale: last modified 700 days ago
        _rule(id_="R2", name="StaleRule", formula="", last_modified=stale_ts),
        # Healthy: formula references known field + modified yesterday
        _rule(
            id_="R3", name="HealthyRule",
            formula="ISBLANK(Known__c)",
            last_modified=now.isoformat(),
        ),
    ]
    result = vm.poll(
        tooling_query=_mk_tooling(records),
        describe_fn=_mk_describe({"Opportunity": ["Known__c"]}),
    )
    assert result["summary"]["total"] == 3
    assert len(result["orphans"]) == 1
    assert len(result["stale"]) == 1
    assert len(result["flagged"]) == 2
    assert len(result["task_ids"]) == 2

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        audits = conn.execute(
            text(
                "SELECT COUNT(*) FROM audit_log WHERE agent_name = 'revops_support' "
                "AND action = 'validation_monitor_poll'"
            )
        ).scalar_one()
        tasks = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'validation_rule_review'"
            )
        ).scalar_one()
    assert audits == 1
    assert tasks == 2


def test_poll_dedupes_tasks_across_runs(_fresh_state):
    records = [_rule(id_="R1", name="OrphanRule", formula="ISBLANK(Missing__c)")]
    vm.poll(
        tooling_query=_mk_tooling(records),
        describe_fn=_mk_describe({"Opportunity": ["Other__c"]}),
    )
    second = vm.poll(
        tooling_query=_mk_tooling(records),
        describe_fn=_mk_describe({"Opportunity": ["Other__c"]}),
    )
    assert second["task_ids"] == []

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        tasks = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'validation_rule_review'"
            )
        ).scalar_one()
    assert tasks == 1


def test_poll_no_issues_no_tasks(_fresh_state):
    now = datetime.now(timezone.utc).isoformat()
    records = [
        _rule(id_="R1", name="HealthyRule",
              formula="ISBLANK(Known__c)", last_modified=now)
    ]
    result = vm.poll(
        tooling_query=_mk_tooling(records),
        describe_fn=_mk_describe({"Opportunity": ["Known__c"]}),
    )
    assert result["flagged"] == []
    assert result["task_ids"] == []

    from shared.db.connection import get_engine
    with get_engine().begin() as conn:
        tasks = conn.execute(
            text(
                "SELECT COUNT(*) FROM tasks WHERE agent_name = 'revops_support' "
                "AND category = 'validation_rule_review'"
            )
        ).scalar_one()
    assert tasks == 0


def _prod_rule_no_formula(
    id_: str = "03dHp000000vhqvIAA",
    name: str = "Block_Test_Int_Changes",
    obj: str = "rh2__PS_Describe__c",
    last_modified: str = "2024-12-16T15:26:23.000+0000",
    owner: str = "Jon Van",
) -> dict[str, Any]:
    """Tooling-API response shape from prod: no ``ErrorConditionFormula``.

    Mirrors an actual ``SELECT Id, ValidationName, Active, ErrorMessage,
    Description, EntityDefinition.QualifiedApiName, LastModifiedDate,
    LastModifiedBy.Name FROM ValidationRule`` row against
    ``salesops@tryloop.ai`` — proves the module doesn't blow up when the
    formula column is absent (the perm-gated case the v0.9 deploy hit).
    """
    return {
        "Id": id_,
        "ValidationName": name,
        "Active": True,
        "ErrorMessage": "You have been stopped from changing Test Integer by a validation rule.",
        "Description": "Prevents the test integer field from being set.",
        "EntityDefinition": {"QualifiedApiName": obj},
        "LastModifiedDate": last_modified,
        "LastModifiedBy": {"Name": owner},
    }


def test_fetch_active_rules_survives_missing_formula_column():
    """Prod column set (no ErrorConditionFormula) should normalize cleanly."""
    rules = vm.fetch_active_rules(
        tooling_query=_mk_tooling([_prod_rule_no_formula()])
    )
    assert len(rules) == 1
    assert rules[0]["formula"] == ""
    assert rules[0]["object"] == "rh2__PS_Describe__c"
    assert rules[0]["owner"] == "Jon Van"


def test_detect_orphans_noop_when_no_formula_available(caplog):
    """When the Tooling query drops ErrorConditionFormula, orphan detection
    degrades to an empty list + a single WARN log."""
    import logging
    rules = vm.fetch_active_rules(
        tooling_query=_mk_tooling([
            _prod_rule_no_formula(id_="R1"),
            _prod_rule_no_formula(id_="R2", name="Another"),
        ])
    )
    with caplog.at_level(logging.WARNING, logger=vm.__name__):
        orphans = vm.detect_orphans(
            rules,
            describe_fn=_mk_describe({"rh2__PS_Describe__c": []}),
        )
    assert orphans == []
    assert any("orphan detection skipped" in rec.message for rec in caplog.records)


def test_poll_end_to_end_without_formula_column(_fresh_state):
    """End-to-end poll against the prod column set: no crashes, stale path
    still works, orphan path returns empty, summary + audit stamped."""
    now = datetime(2026, 4, 16, tzinfo=timezone.utc)
    stale_ts = (now - timedelta(days=700)).isoformat()
    records = [
        _prod_rule_no_formula(id_="R1", name="Recent"),
        _prod_rule_no_formula(id_="R2", name="Stale", last_modified=stale_ts),
    ]
    result = vm.poll(
        tooling_query=_mk_tooling(records),
        describe_fn=_mk_describe({"rh2__PS_Describe__c": []}),
    )
    assert result["summary"]["total"] == 2
    assert result["orphans"] == []
    assert len(result["stale"]) == 1
    assert len(result["task_ids"]) == 1


def test_dispatcher_routes_validation_monitor():
    """Smoke: @oo admin validation monitor should dispatch into the module."""
    import asyncio
    from unittest.mock import patch
    from agents.revops_support.agent import RevOpsSupportAgent

    agent = RevOpsSupportAgent()
    with patch.object(vm, "poll", return_value={
        "summary": {"total": 0, "by_object": {}, "by_owner": {}},
        "orphans": [], "stale": [], "flagged": [], "task_ids": [],
    }) as mocked:
        resp = asyncio.run(agent.handle("", {"text": "validation monitor"}))
    assert mocked.called
    assert "validation" in resp["text"].lower()
