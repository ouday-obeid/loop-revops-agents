"""M5 — Renewal stall monitor tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.cs.renewal import stall_monitor
from shared.db.connection import get_engine


class FakeSlack:
    def __init__(self): self.sent: list[tuple[str, str]] = []
    def send(self, channel, text_, blocks=None):
        self.sent.append((channel, text_)); return {"ok": True, "ts": "0", "channel": channel}


class FakeSf:
    def __init__(self, opps): self.opps = opps
    def soql_query(self, q, **_): return {"records": self.opps}


def _opp(oid, days_since_change, *, stage="Negotiation", account="Acme"):
    last = (datetime.now(timezone.utc) - timedelta(days=days_since_change)).isoformat()
    return {
        "Id": oid,
        "Name": f"{account} Renewal",
        "AccountId": "001A",
        "Account": {"Name": account},
        "OwnerId": "005X",
        "StageName": stage,
        "LastStageChangeDate": last,
        "LastModifiedDate": last,
        "Zen_Contract_End_Date__c": "2026-12-31",
    }


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:renewal_stall:%'"))


@pytest.fixture(autouse=True)
def _clean():
    _clear(); yield; _clear()


@pytest.mark.asyncio
async def test_below_threshold_no_action():
    sf = FakeSf([_opp("006A", 10)])
    slack = FakeSlack()
    counters = await stall_monitor.run_sweep(sf_mcp=sf, slack_sender=slack)
    assert counters["warn"] == 0
    assert counters["escalate"] == 0
    assert slack.sent == []


@pytest.mark.asyncio
async def test_warn_at_14_days():
    sf = FakeSf([_opp("006A", 15)])
    slack = FakeSlack()
    counters = await stall_monitor.run_sweep(sf_mcp=sf, slack_sender=slack)
    assert counters["warn"] == 1
    assert counters["escalate"] == 0
    assert len(slack.sent) == 1
    assert "15d" in slack.sent[0][1]
    assert "@jackie" not in slack.sent[0][1]

    engine = get_engine()
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT priority, assignee FROM tasks WHERE source LIKE 'cs:renewal_stall:%'")
        ).mappings().first()
    assert task["priority"] == "high"
    assert task["assignee"] == "blaine"


@pytest.mark.asyncio
async def test_escalate_at_30_days_pings_jackie():
    sf = FakeSf([_opp("006A", 45)])
    slack = FakeSlack()
    counters = await stall_monitor.run_sweep(sf_mcp=sf, slack_sender=slack)
    assert counters["escalate"] == 1
    assert counters["warn"] == 0
    assert "@jackie" in slack.sent[0][1]

    engine = get_engine()
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT priority, assignee FROM tasks WHERE source LIKE 'cs:renewal_stall:%'")
        ).mappings().first()
    assert task["priority"] == "urgent"
    assert task["assignee"] == "jackie"


@pytest.mark.asyncio
async def test_idempotent_within_same_day():
    sf = FakeSf([_opp("006A", 20)])
    slack = FakeSlack()
    await stall_monitor.run_sweep(sf_mcp=sf, slack_sender=slack)
    counters2 = await stall_monitor.run_sweep(sf_mcp=FakeSf([_opp("006A", 21)]), slack_sender=slack)
    assert counters2["skipped_today"] == 1
    assert counters2["warn"] == 0
    # Only one slack message despite two runs.
    assert len(slack.sent) == 1


@pytest.mark.asyncio
async def test_missing_stage_change_date_falls_back_to_modified():
    now = datetime.now(timezone.utc)
    last = (now - timedelta(days=20)).isoformat()
    opp = _opp("006A", 0)
    opp["LastStageChangeDate"] = None
    opp["LastModifiedDate"] = last
    sf = FakeSf([opp])
    slack = FakeSlack()
    counters = await stall_monitor.run_sweep(sf_mcp=sf, slack_sender=slack)
    assert counters["warn"] == 1
