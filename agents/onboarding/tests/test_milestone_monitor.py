"""Milestone monitor — business-day math, stall evaluation, dedup."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from agents.onboarding import milestone_monitor as mm


# ---------- Business-day math ----------

@pytest.mark.parametrize(
    "start,end,expected",
    [
        (date(2026, 4, 13), date(2026, 4, 13), 0),          # same day
        (date(2026, 4, 13), date(2026, 4, 14), 1),          # Mon→Tue
        (date(2026, 4, 10), date(2026, 4, 13), 1),          # Fri→Mon (Sat+Sun skipped)
        (date(2026, 4, 6),  date(2026, 4, 13), 5),          # full week
        (date(2026, 4, 10), date(2026, 4, 17), 5),          # Fri→next Fri
        (date(2026, 4, 14), date(2026, 4, 13), 0),          # end <= start
    ],
)
def test_business_days_between(start, end, expected):
    assert mm.business_days_between(start, end) == expected


# ---------- Last-advanced inference ----------

def test_last_advanced_prefers_ds_family_over_last_modified():
    rec = {
        "LastModifiedDate": "2026-04-13T10:00:00Z",
        "DS_Kickoff_Status_Scheduled__c": "2026-04-10",
        "DS_Overall_Onboarding_Status_In_Progress__c": "2026-04-12",
    }
    last = mm._last_advanced_from_ds_family(rec)
    assert last == datetime(2026, 4, 12, 0, 0, tzinfo=timezone.utc)


def test_last_advanced_returns_none_without_ds_family():
    rec = {"LastModifiedDate": "2026-04-13T10:00:00Z"}
    assert mm._last_advanced_from_ds_family(rec) is None


# ---------- evaluate_stall ----------

@pytest.fixture
def _now():
    return datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)


def test_evaluate_stall_under_threshold_returns_none(_now):
    rec = {
        "Id": "a01A",
        "Name": "Acme",
        "JK_Onboarding_Stage__c": "Initial Onboarding Scheduled",
        "Overall_Onboarding_Status__c": "In Progress",
        "LastModifiedDate": "2026-04-10T10:00:00Z",  # 1 business day ago
    }
    assert mm.evaluate_stall(rec, now=_now) is None


def test_evaluate_stall_over_threshold_flags(_now):
    rec = {
        "Id": "a01A",
        "Name": "Acme",
        "OwnerId": "005X",
        "CSM_2__c": "005Y",
        "Account__c": "001A",
        "JK_Onboarding_Stage__c": "Initial Onboarding Scheduled",
        "Overall_Onboarding_Status__c": "In Progress",
        "DS_Overall_Onboarding_Status_In_Progress__c": "2026-04-03",  # >5 bdays
    }
    stall = mm.evaluate_stall(rec, now=_now)
    assert stall is not None
    assert stall.onboarding_id == "a01A"
    assert stall.days_stalled >= 5
    assert stall.owner_id == "005X"


def test_evaluate_stall_uses_last_modified_fallback(_now):
    rec = {
        "Id": "a01A",
        "Name": "Acme",
        "JK_Onboarding_Stage__c": "Getting Access",
        "Overall_Onboarding_Status__c": "Not Started",
        "LastModifiedDate": "2026-04-02T10:00:00Z",  # >5 bdays, no DS stamps
    }
    stall = mm.evaluate_stall(rec, now=_now)
    assert stall is not None


def test_evaluate_stall_record_with_no_timestamps_returns_none():
    rec = {"Id": "a01A", "Name": "Acme"}
    assert mm.evaluate_stall(rec) is None


# ---------- dedup ----------

def test_stage_fingerprint_changes_on_stage_change():
    a = {"JK_Onboarding_Stage__c": "Getting Access",
         "Overall_Onboarding_Status__c": "In Progress"}
    b = {"JK_Onboarding_Stage__c": "Initial Onboarding Session Needed",
         "Overall_Onboarding_Status__c": "In Progress"}
    assert mm._stage_fingerprint(a) != mm._stage_fingerprint(b)


def test_record_alert_then_recently_alerted_is_true():
    mm._ensure_dedup_table()
    mm._record_alert("a01DEDUP1", "jk=x;overall=y")
    assert mm._recently_alerted("a01DEDUP1", "jk=x;overall=y") is True


def test_recently_alerted_false_for_unseen_pair():
    mm._ensure_dedup_table()
    assert mm._recently_alerted("a01NEVER_SEEN", "jk=x;overall=y") is False


# ---------- find_stalls / scan ----------

@pytest.mark.asyncio
async def test_find_stalls_returns_list_of_dicts(fake_sf_monkeypatch, _now):
    rec = {
        "Id": "a01STALL",
        "Name": "Acme",
        "OwnerId": "005X",
        "CSM_2__c": None,
        "Account__c": "001A",
        "JK_Onboarding_Stage__c": "Getting Access",
        "Overall_Onboarding_Status__c": "In Progress",
        "DS_Overall_Onboarding_Status_In_Progress__c": "2026-04-01",
    }
    fake_sf_monkeypatch.queue_soql({"records": [rec], "totalSize": 1, "done": True})
    results = await mm.find_stalls(now=_now)
    assert len(results) == 1
    assert results[0]["id"] == "a01STALL"
    assert results[0]["days"] >= 5


@pytest.mark.asyncio
async def test_scan_dedups_within_72h(fake_sf_monkeypatch, _now, monkeypatch):
    rec = {
        "Id": "a01SCAN",
        "Name": "Acme",
        "OwnerId": None,
        "CSM_2__c": None,
        "Account__c": "001A",
        "JK_Onboarding_Stage__c": "Getting Access",
        "Overall_Onboarding_Status__c": "In Progress",
        "DS_Overall_Onboarding_Status_In_Progress__c": "2026-04-01",
    }
    # Pre-seed dedup table so first scan counts as already alerted.
    mm._ensure_dedup_table()
    mm._record_alert("a01SCAN", mm._stage_fingerprint(rec))

    fake_sf_monkeypatch.queue_soql({"records": [rec], "totalSize": 1, "done": True})
    sends: list = []

    class Silent:
        def send(self, *a, **kw):
            sends.append(a)
            return {"ok": True}

    from shared import slack_dispatcher
    monkeypatch.setattr(slack_dispatcher, "SlackSender", lambda: Silent())

    summary = await mm.scan(now=_now)
    assert summary["skipped_dedup"] == 1
    assert summary["alerted"] == 0
    assert not sends  # dedup blocked the post
