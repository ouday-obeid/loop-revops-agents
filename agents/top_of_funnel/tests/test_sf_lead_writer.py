"""D5 tests for sf_lead_writer — schema probe, payload, dedup, TLO, create."""
from __future__ import annotations

from typing import Any

import pytest

from agents.top_of_funnel import sf_lead_writer as writer


# ------------------------------------------------------------ schema probe


def _fake_describe(existing: set[str]):
    def fn(name: str) -> dict[str, Any]:
        assert name == "Lead"
        return {"fields": [{"name": n} for n in existing]}

    return fn


def test_describe_returns_only_known_customs():
    fn = _fake_describe({"ICP_Score__c", "Brand__c", "UnrelatedField__c"})
    present = writer.describe_lead_custom_fields(fn)
    assert present == {"ICP_Score__c", "Brand__c"}


def test_describe_empty_when_sf_errors():
    def boom(name: str):
        raise RuntimeError("sf connect fail")

    assert writer.describe_lead_custom_fields(boom) == set()


def test_describe_empty_when_no_custom_fields():
    fn = _fake_describe(set())
    assert writer.describe_lead_custom_fields(fn) == set()


# ------------------------------------------------------------ build_payload


def _lead_fixture(**overrides: Any) -> dict[str, Any]:
    base = {
        "domain": "franchisee.com",
        "company_name": "Franchisee Co",
        "email": "jane@franchisee.com",
        "first_name": "Jane",
        "last_name": "Doe",
        "title": "VP Operations",
        "phone": "+15551234567",
        "icp_score": 82,
        "icp_tier": "A",
        "brand": "Arby's",
        "ownership_type": "franchise_group",
        "location_count": 47,
    }
    base.update(overrides)
    return base


def test_build_payload_all_customs_present_no_description_fallback():
    present = {"ICP_Score__c", "ICP_Tier__c", "Brand__c", "Ownership_Type__c", "Location_Count__c"}
    out = writer.build_payload(_lead_fixture(), present_custom_fields=present)
    assert out.fields["ICP_Score__c"] == 82
    assert out.fields["Brand__c"] == "Arby's"
    assert out.fields["Location_Count__c"] == 47
    assert out.fallback_used is False
    assert "Description" not in out.fields
    assert out.missing_fields == []


def test_build_payload_all_customs_missing_packs_into_description():
    out = writer.build_payload(_lead_fixture(), present_custom_fields=set())
    assert out.fallback_used is True
    desc = out.fields["Description"]
    assert desc.startswith("[Loop ToF]")
    assert "ICP:82" in desc
    assert "Brand:Arby's" in desc
    assert "Locations:47" in desc
    # None of the custom fields should be in the payload.
    for cf in ("ICP_Score__c", "Brand__c"):
        assert cf not in out.fields
    assert set(out.missing_fields) >= {"ICP_Score__c", "Brand__c"}


def test_build_payload_partial_customs():
    """Only ICP_Score__c exists — others fall through to Description."""
    present = {"ICP_Score__c"}
    out = writer.build_payload(_lead_fixture(), present_custom_fields=present)
    assert out.fields["ICP_Score__c"] == 82
    assert out.fallback_used is True
    desc = out.fields["Description"]
    assert "Brand:Arby's" in desc
    assert "ICP:" not in desc  # that one WAS written to a real field


def test_build_payload_preserves_existing_description():
    lead = _lead_fixture(description="Pre-existing note from SDR.")
    out = writer.build_payload(lead, present_custom_fields=set())
    assert "Pre-existing note from SDR." in out.fields["Description"]
    assert out.fields["Description"].startswith("[Loop ToF]")


def test_build_payload_defaults_for_missing_names():
    """No FirstName / LastName → 'Unknown' placeholders so SF create doesn't
    reject (LastName is required on Lead)."""
    lead = _lead_fixture(first_name=None, last_name=None)
    out = writer.build_payload(lead, present_custom_fields=set())
    assert out.fields["FirstName"] == "Unknown"
    assert out.fields["LastName"] == "Unknown"


def test_build_payload_owner_and_tlo():
    """Lead-side TLO reference defaults to the fully-spelled
    `Top_Level_Organization__c` — matches Opportunity convention and the
    revagents sandbox schema (verified D6, 2026-04-13)."""
    out = writer.build_payload(
        _lead_fixture(),
        present_custom_fields=set(),
        tlo_id="a0X00000000xyz",
        owner_id="005ABC",
    )
    assert out.fields["OwnerId"] == "005ABC"
    assert out.fields["Top_Level_Organization__c"] == "a0X00000000xyz"
    # And the short-name Account-convention must NOT leak onto Lead.
    assert "Top_Level_Org__c" not in out.fields


def test_build_payload_sets_lead_source_default():
    out = writer.build_payload(_lead_fixture(), present_custom_fields=set())
    assert out.fields["LeadSource"].startswith("AI Prospecting")


# ---------------------------------------------- fallback field configurability


def test_build_payload_default_fallback_is_description(monkeypatch):
    """Regression: with `TOF_LEAD_FALLBACK_FIELD` unset the writer targets
    `Description` exactly as before — preserves prod behavior."""
    monkeypatch.delenv("TOF_LEAD_FALLBACK_FIELD", raising=False)
    out = writer.build_payload(_lead_fixture(), present_custom_fields=set())
    assert out.fallback_used is True
    assert "Description" in out.fields
    assert out.fields["Description"].startswith("[Loop ToF]")


def test_build_payload_respects_custom_fallback_field(monkeypatch):
    """Sandboxes can redirect the fallback to a custom long-text field like
    `Lead_Notes__c` by setting the env var."""
    monkeypatch.setenv("TOF_LEAD_FALLBACK_FIELD", "Lead_Notes__c")
    out = writer.build_payload(_lead_fixture(), present_custom_fields=set())
    assert out.fallback_used is True
    assert "Description" not in out.fields  # default target not touched
    assert "Lead_Notes__c" in out.fields
    assert out.fields["Lead_Notes__c"].startswith("[Loop ToF]")
    assert "Brand:Arby's" in out.fields["Lead_Notes__c"]


def test_build_payload_disables_fallback_when_env_empty(monkeypatch):
    """Empty string is the opt-out signal: the create payload should carry
    NO fallback-field write (neither Description nor anything else), but the
    rest of the lead (names, email, company) still goes through intact.
    `missing_fields` still reports the gap so the caller can audit."""
    monkeypatch.setenv("TOF_LEAD_FALLBACK_FIELD", "")
    out = writer.build_payload(_lead_fixture(), present_custom_fields=set())
    assert out.fallback_used is False
    assert "Description" not in out.fields
    # Every custom field was missing — expect all to be reported.
    assert set(out.missing_fields) == {
        "ICP_Score__c", "ICP_Tier__c", "Brand__c", "Ownership_Type__c",
        "Location_Count__c",
    }
    # Base lead fields are still present.
    assert out.fields["Email"] == "jane@franchisee.com"
    assert out.fields["LastName"] == "Doe"
    assert out.fields["Company"] == "Franchisee Co"


def test_build_payload_disabled_fallback_drops_caller_description(monkeypatch):
    """When the operator has opted out of the fallback we don't presume a
    Description field exists — even caller-supplied descriptions are dropped
    to avoid INVALID_FIELD on schema-stripped sandboxes."""
    monkeypatch.setenv("TOF_LEAD_FALLBACK_FIELD", "")
    lead = _lead_fixture(description="Existing SDR note.")
    out = writer.build_payload(lead, present_custom_fields=set())
    assert "Description" not in out.fields
    assert out.fallback_used is False


def test_build_payload_explicit_fallback_field_overrides_env(monkeypatch):
    """Caller-passed `fallback_field` beats the env var — exposes the knob
    for tests/ops tooling that needs to force a single run's target."""
    monkeypatch.setenv("TOF_LEAD_FALLBACK_FIELD", "Notes_Env__c")
    out = writer.build_payload(
        _lead_fixture(),
        present_custom_fields=set(),
        fallback_field="Caller_Chose__c",
    )
    assert "Notes_Env__c" not in out.fields
    assert "Caller_Chose__c" in out.fields
    assert out.fields["Caller_Chose__c"].startswith("[Loop ToF]")


def test_create_lead_honors_disabled_fallback_end_to_end(monkeypatch):
    """End-to-end sandbox-mimic: describe reports NO custom fields, env var
    opts out of the fallback — create_lead should still succeed without
    trying to write Description."""
    monkeypatch.setenv("TOF_LEAD_FALLBACK_FIELD", "")

    captured: dict[str, Any] = {}

    def fake_q(q: str):
        return {"records": []}

    def fake_create(sobject, fields, **kw):
        captured["fields"] = fields
        return {"id": "00Q0SANDBOX"}

    out = writer.create_lead(
        _lead_fixture(),
        approval_gate_id=9,
        describe_fn=_fake_describe(set()),
        sf_query=fake_q,
        create_fn=fake_create,
    )
    assert out["sf_id"] == "00Q0SANDBOX"
    assert out["fallback_used"] is False
    assert "Description" not in captured["fields"]
    # None of the custom fields should be in the payload either — sandbox
    # claimed none exist — but the write still landed.
    for cf in ("ICP_Score__c", "Brand__c", "Ownership_Type__c"):
        assert cf not in captured["fields"]


def test_describe_soft_fail_still_allows_disabled_fallback(monkeypatch):
    """Schema probe exception path combined with disabled fallback — both
    graceful-degradation paths compose cleanly. Mirrors the sandbox case
    where describe itself can fail AND Description is absent."""
    monkeypatch.setenv("TOF_LEAD_FALLBACK_FIELD", "")

    def boom(name: str):
        raise RuntimeError("describe blew up")

    present = writer.describe_lead_custom_fields(boom)
    assert present == set()

    # Now run build_payload with the empty-set result — no crash, no fallback.
    out = writer.build_payload(_lead_fixture(), present_custom_fields=present)
    assert out.fallback_used is False
    assert "Description" not in out.fields


# ------------------------------------------------------------ TLO lookup


def test_find_tlo_by_domain():
    calls: list[str] = []

    def fake_q(q: str):
        calls.append(q)
        return {"records": [{"Id": "a0X00xyz"}]}

    assert writer.find_tlo_id(
        domain="franchisee.com", company_name=None, sf_query=fake_q
    ) == "a0X00xyz"
    assert "Domain__c" in calls[0]


def test_find_tlo_by_name():
    def fake_q(q: str):
        return {"records": [{"Id": "a0X00abc"}]}

    assert writer.find_tlo_id(
        domain=None, company_name="Acme", sf_query=fake_q
    ) == "a0X00abc"


def test_find_tlo_none_when_no_records():
    def fake_q(q: str):
        return {"records": []}

    assert writer.find_tlo_id(domain="x.com", company_name=None, sf_query=fake_q) is None


def test_find_tlo_sf_error_returns_none():
    def boom(q: str):
        raise RuntimeError("sf crashed")

    assert writer.find_tlo_id(domain="x.com", company_name=None, sf_query=boom) is None


def test_find_tlo_both_ids_returns_first():
    seen: list[str] = []

    def fake_q(q: str):
        seen.append(q)
        return {"records": [{"Id": "a0X00together"}]}

    assert writer.find_tlo_id(
        domain="x.com", company_name="X Co", sf_query=fake_q
    ) == "a0X00together"
    # Single query with both clauses OR'd.
    assert len(seen) == 1
    assert "OR" in seen[0]


def test_find_tlo_no_identifiers():
    assert writer.find_tlo_id(domain=None, company_name=None) is None


# ----------------------------------------------------------------- dedup


def test_dedup_hits_on_lead_email():
    def fake_q(q: str):
        if "FROM Lead" in q:
            return {"records": [{"Id": "00Q0000"}]}
        return {"records": []}

    r = writer.check_duplicate(email="x@y.com", domain="y.com", sf_query=fake_q)
    assert r.is_duplicate
    assert r.existing_kind == "lead"
    assert r.existing_id == "00Q0000"


def test_dedup_hits_on_contact_email():
    def fake_q(q: str):
        if "FROM Lead" in q:
            return {"records": []}
        if "FROM Contact" in q:
            return {"records": [{"Id": "0030000"}]}
        return {"records": []}

    r = writer.check_duplicate(email="x@y.com", domain="y.com", sf_query=fake_q)
    assert r.is_duplicate
    assert r.existing_kind == "contact"


def test_dedup_hits_on_account_website():
    def fake_q(q: str):
        if "FROM Account" in q:
            return {"records": [{"Id": "001ABC"}]}
        return {"records": []}

    r = writer.check_duplicate(email=None, domain="acme.com", sf_query=fake_q)
    assert r.is_duplicate
    assert r.existing_kind == "account"


def test_dedup_no_match():
    def fake_q(q: str):
        return {"records": []}

    r = writer.check_duplicate(email="new@x.com", domain="x.com", sf_query=fake_q)
    assert r.is_duplicate is False


def test_dedup_probe_failure_fail_open():
    def boom(q: str):
        raise RuntimeError("soql exploded")

    r = writer.check_duplicate(email="x@y.com", domain="y.com", sf_query=boom)
    assert r.is_duplicate is False
    assert "dedup_probe_failed" in r.reason


def test_dedup_no_identifiers():
    r = writer.check_duplicate(email=None, domain=None)
    assert r.is_duplicate is False


# ---------------------------------------------------------------- create_lead


def test_create_lead_skips_on_contact_dupe():
    def fake_q(q: str):
        if "FROM Contact" in q:
            return {"records": [{"Id": "0030001"}]}
        return {"records": []}

    creates: list[tuple[str, dict]] = []

    def fake_create(sobject, fields, **kw):
        creates.append((sobject, fields))
        return {"id": "should_not_be_called"}

    out = writer.create_lead(
        _lead_fixture(),
        approval_gate_id=7,
        describe_fn=_fake_describe(set()),
        sf_query=fake_q,
        create_fn=fake_create,
    )
    assert out["skipped"] is True
    assert out["dedup"]["existing_kind"] == "contact"
    assert creates == []  # never called


def test_create_lead_happy_path_uses_real_fields():
    present = {"ICP_Score__c", "ICP_Tier__c", "Brand__c", "Ownership_Type__c", "Location_Count__c"}

    def fake_q(q: str):
        return {"records": []}  # no dedup, no TLO

    captured: dict[str, Any] = {}

    def fake_create(sobject, fields, **kw):
        captured["sobject"] = sobject
        captured["fields"] = fields
        captured["kw"] = kw
        return {"id": "00Q9ABCDEFG12345"}

    out = writer.create_lead(
        _lead_fixture(),
        approval_gate_id=42,
        describe_fn=_fake_describe(present),
        sf_query=fake_q,
        create_fn=fake_create,
    )
    assert out["sf_id"] == "00Q9ABCDEFG12345"
    assert out["fallback_used"] is False
    assert out["skipped"] is False
    assert captured["sobject"] == "Lead"
    assert captured["kw"]["approval_gate_id"] == 42
    assert captured["kw"]["agent_name"] == "top_of_funnel"
    assert captured["fields"]["ICP_Score__c"] == 82
    assert "Description" not in captured["fields"]


def test_create_lead_falls_back_to_description_when_no_customs():
    def fake_q(q: str):
        return {"records": []}

    captured: dict[str, Any] = {}

    def fake_create(sobject, fields, **kw):
        captured["fields"] = fields
        return {"id": "00Q0NEWID"}

    out = writer.create_lead(
        _lead_fixture(),
        approval_gate_id=3,
        describe_fn=_fake_describe(set()),
        sf_query=fake_q,
        create_fn=fake_create,
    )
    assert out["fallback_used"] is True
    assert captured["fields"]["Description"].startswith("[Loop ToF]")
    assert "ICP_Score__c" not in captured["fields"]


def test_create_lead_links_tlo_when_present():
    """End-to-end TLO linkage: SOQL hits the Top_Level_Organization__c
    SObject, the returned Id lands on the Lead's fully-spelled reference
    field (not the short-name Account-side convention)."""
    def fake_q(q: str):
        if "Top_Level_Organization__c" in q:
            return {"records": [{"Id": "a0Xtop"}]}
        return {"records": []}

    captured: dict[str, Any] = {}

    def fake_create(sobject, fields, **kw):
        captured["fields"] = fields
        return {"id": "00Qxyz"}

    out = writer.create_lead(
        _lead_fixture(),
        approval_gate_id=1,
        describe_fn=_fake_describe(set()),
        sf_query=fake_q,
        create_fn=fake_create,
    )
    assert captured["fields"]["Top_Level_Organization__c"] == "a0Xtop"
    assert "Top_Level_Org__c" not in captured["fields"]
    assert out["tlo_id"] == "a0Xtop"


def test_build_payload_respects_lead_tlo_field_env(monkeypatch):
    """Prod/sandbox divergence escape hatch — `TOF_LEAD_TLO_FIELD` overrides
    the default fully-spelled name with whatever the target org expects."""
    monkeypatch.setenv("TOF_LEAD_TLO_FIELD", "Top_Level_Org__c")
    out = writer.build_payload(
        _lead_fixture(),
        present_custom_fields=set(),
        tlo_id="a0XOVERRIDE",
    )
    assert out.fields["Top_Level_Org__c"] == "a0XOVERRIDE"
    assert "Top_Level_Organization__c" not in out.fields


def test_build_payload_lead_tlo_field_empty_env_uses_default(monkeypatch):
    """Empty string env var → default (NOT disabled). TLO is a foreign-key
    lookup, not a text field — there's no meaningful 'disabled' state, an
    unusable field would just crash SF. Empty env is treated as 'unset'."""
    monkeypatch.setenv("TOF_LEAD_TLO_FIELD", "")
    out = writer.build_payload(
        _lead_fixture(),
        present_custom_fields=set(),
        tlo_id="a0XDEFAULT",
    )
    assert out.fields["Top_Level_Organization__c"] == "a0XDEFAULT"
