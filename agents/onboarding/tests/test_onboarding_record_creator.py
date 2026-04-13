"""Field mapping, forbidden-field guard, and create path."""
from __future__ import annotations

import pytest

from agents.onboarding import onboarding_record_creator as creator


def test_build_fields_sets_initial_stage_picklists(make_opp):
    fields = creator.build_fields(make_opp())
    assert fields["Overall_Onboarding_Status__c"] == "Not Started"
    assert fields["JK_Onboarding_Stage__c"] == "Getting Access"
    assert fields["Kickoff_Status__c"] == "Not Scheduled"


def test_build_fields_defaults_required_booleans_false(make_opp):
    fields = creator.build_fields(make_opp())
    for name in creator.REQUIRED_BOOLEANS:
        assert fields[name] is False


def test_build_fields_names_record_from_account(make_opp):
    fields = creator.build_fields(make_opp(account_name="Acme Restaurants"))
    assert fields["Name"] == "Acme Restaurants Onboarding"


def test_build_fields_unnamed_account_still_creates_name(make_opp):
    opp = make_opp()
    opp["Account"] = {"Name": ""}
    fields = creator.build_fields(opp)
    assert fields["Name"].endswith("Onboarding")


def test_build_fields_omits_owner_when_null(make_opp):
    fields = creator.build_fields(make_opp(owner_id=None))
    assert "OwnerId" not in fields


def test_build_fields_sets_owner_when_present(make_opp):
    fields = creator.build_fields(make_opp(owner_id="005CSM"))
    assert fields["OwnerId"] == "005CSM"


def test_build_fields_never_writes_formula_fields(make_opp):
    fields = creator.build_fields(make_opp())
    assert set(fields).isdisjoint(creator.FORBIDDEN_FIELDS)


def test_assert_no_forbidden_raises_on_overlap():
    with pytest.raises(ValueError, match="formula fields"):
        creator._assert_no_forbidden({"ACV__c": 1000})


def test_create_from_opp_posts_to_sf_and_cs_channel(
    make_opp, fake_sf_monkeypatch, seed_gate, monkeypatch
):
    gate_id = seed_gate(action_type="single_record_update", status="approved")

    posted = []

    class FakeSender:
        def send(self, channel, text_, blocks=None):
            posted.append((channel, text_))
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: FakeSender())

    result = creator.create_from_opp(make_opp(), gate_id=gate_id)
    assert result["success"] is True
    # The fake MCP records exactly one create.
    assert len(fake_sf_monkeypatch.created) == 1
    # Slack post contains the account name.
    assert any("Acme Restaurants" in text_ for _, text_ in posted)


def test_create_from_opp_unassigned_triggers_enforcer(
    make_opp, fake_sf_monkeypatch, seed_gate, monkeypatch
):
    gate_id = seed_gate(action_type="single_record_update", status="approved")

    called = {}

    def fake_handle_created(opp, onboarding_id):
        called["opp"] = opp["Id"]
        called["ob"] = onboarding_id
        return {"posted": True}

    from agents.onboarding import csm_enforcer
    monkeypatch.setattr(csm_enforcer, "handle_created", fake_handle_created)

    # Minimize Slack noise from the notify path.
    class Silent:
        def send(self, *_a, **_kw):
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())

    creator.create_from_opp(make_opp(owner_id=None), gate_id=gate_id)
    assert "opp" in called
