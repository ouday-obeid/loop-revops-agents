"""M8 — Weekly CS digest tests."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from agents.cs.reports import weekly
from shared.db.connection import get_engine


class FakeSlack:
    def __init__(self): self.sent: list[tuple[str, str]] = []
    def send(self, channel, text_, blocks=None):
        self.sent.append((channel, text_)); return {"ok": True, "ts": "0", "channel": channel}


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_account_health"))
        conn.execute(text("DELETE FROM cs_churn_risk"))
        conn.execute(text("DELETE FROM cs_renewal_state"))
        conn.execute(text("DELETE FROM tasks WHERE agent_name IN ('cs','revops_support')"))
        conn.execute(text("DELETE FROM integration_health"))


def _seed_risk(account_id, score, tier, contributions, name="Acme", created_days_ago=1):
    engine = get_engine()
    when = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health (account_id, name, score, checked_at)
                   VALUES (:a, :n, 50, :w)"""
            ),
            {"a": account_id, "n": name, "w": when},
        )
        conn.execute(
            text(
                """INSERT INTO cs_churn_risk (account_id, score, tier, factors_json, created_at)
                   VALUES (:a, :s, :t, :f, :w)"""
            ),
            {
                "a": account_id, "s": score, "t": tier,
                "f": json.dumps({"contributions": contributions}),
                "w": when,
            },
        )


@pytest.fixture(autouse=True)
def _clean():
    _clear(); yield; _clear()


def test_report_lists_top_risks_tier_70_plus():
    _seed_risk("001A", 90, 85, {"health_drop": 35, "case_spike": 15, "nps": 4}, name="Big Co")
    _seed_risk("001B", 55, 50, {"health_drop": 20}, name="SkipMe")  # tier 50 excluded
    _seed_risk("001C", 78, 70, {"nps": 4, "health_drop": 35}, name="MedCo")

    md = weekly.build_report()
    assert "Big Co" in md
    assert "MedCo" in md
    assert "SkipMe" not in md
    # Sorted by score desc
    assert md.index("Big Co") < md.index("MedCo")


def test_report_handles_no_risks():
    md = weekly.build_report()
    assert "No accounts scored tier ≥70" in md


def test_renewal_pipeline_counts():
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_renewal_state
                     (opportunity_id, account_id, stage, provisional, created_at)
                   VALUES ('006A', '001A', 'Renewal Outreach', 0, :n)"""
            ),
            {"n": now - timedelta(days=2)},
        )
        conn.execute(
            text(
                """INSERT INTO cs_renewal_state
                     (opportunity_id, account_id, stage, provisional, created_at)
                   VALUES ('006B', '001B', 'Qualification', 1, :n)"""
            ),
            {"n": now - timedelta(days=1)},
        )

    md = weekly.build_report()
    assert "New renewal opps this week: **2**" in md
    assert "Provisional" in md
    assert "1" in md  # provisional count


def test_open_stalls_counted():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, status, category, source)
                   VALUES ('cs', 't', 'pending', 'renewal_stall', 'cs:renewal_stall:X')"""
            )
        )
    md = weekly.build_report()
    assert "Open stalls (≥14d): 1" in md


def test_expansion_counts():
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for i in range(3):
            conn.execute(
                text(
                    """INSERT INTO tasks (agent_name, title, status, category, source, created_at)
                       VALUES ('cs', 't', 'pending', 'expansion',
                               :s, :n)"""
                ),
                {"s": f"cs:expansion:X{i}", "n": now - timedelta(days=1)},
            )
    md = weekly.build_report()
    assert "New expansion tasks this week: **3**" in md


def test_uid_match_rate_surfaced():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO integration_health (integration, status, error_message, checked_at)
                   VALUES ('vitally_uid_resolution', 'degraded',
                           'UID match rate 80.0% below 95% threshold', :n)"""
            ),
            {"n": datetime.now(timezone.utc)},
        )
    md = weekly.build_report()
    assert "Vitally UID match: **degraded**" in md
    assert "80.0%" in md


def test_nps_freshness_percentage():
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        for i, days in enumerate([1, 5, 40, 90]):
            conn.execute(
                text(
                    """INSERT INTO cs_account_health (account_id, nps_at, checked_at)
                       VALUES (:a, :n, :now)"""
                ),
                {"a": f"A{i}", "n": now - timedelta(days=days), "now": now},
            )
    md = weekly.build_report()
    assert "50%" in md  # 2 of 4 fresh


@pytest.mark.asyncio
async def test_send_posts_to_channel_only_when_no_jackie_id(monkeypatch):
    monkeypatch.delenv("JACKIE_SLACK_USER_ID", raising=False)
    slack = FakeSlack()
    result = await weekly.send(slack_sender=slack)
    assert result["posted_to"] == ["#agent-cs-log"]
    assert len(slack.sent) == 1


@pytest.mark.asyncio
async def test_send_dm_to_jackie_when_configured(monkeypatch):
    monkeypatch.setenv("JACKIE_SLACK_USER_ID", "U_JACKIE")
    slack = FakeSlack()
    result = await weekly.send(slack_sender=slack)
    assert "#agent-cs-log" in result["posted_to"]
    assert "U_JACKIE" in result["posted_to"]
    assert len(slack.sent) == 2
