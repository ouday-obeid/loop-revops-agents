"""M1 — Health monitor end-to-end test with fake Vitally + fake SlackSender."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest
from sqlalchemy import text

from agents.cs.health import health_monitor
from shared.db.connection import get_engine


class FakeVitally:
    def __init__(self, accounts: list[dict[str, Any]]):
        self._accounts = accounts

    async def iter_accounts(self, *, page_size: int = 100) -> AsyncIterator[dict]:
        for a in self._accounts:
            yield a

    async def close(self) -> None:  # pragma: no cover
        pass


class FakeSlackSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, channel: str, text_: str, blocks=None) -> dict:
        self.sent.append((channel, text_))
        return {"ok": True, "ts": "0", "channel": channel}


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_account_health"))
        conn.execute(text("DELETE FROM cs_account_health_history"))
        conn.execute(text("DELETE FROM integration_health WHERE integration LIKE 'vitally%'"))
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:uid_resolver:%'"))


def _make_account(
    vid: str,
    sf_id: str | None,
    *,
    health: float | None = 80.0,
    nps: int | None = 9,
    name: str = "Acme",
) -> dict:
    acct: dict[str, Any] = {"id": vid, "name": name}
    if sf_id is not None:
        acct["externalId"] = sf_id
    if health is not None:
        acct["healthScore"] = {"current": health}
    if nps is not None:
        acct["npsLatest"] = {"score": nps, "respondedAt": "2026-04-10T00:00:00Z"}
    return acct


@pytest.mark.asyncio
async def test_poll_upserts_and_records_match_rate():
    _clear()
    fake = FakeVitally(
        [
            _make_account("v1", "001SF1", health=85.0, nps=9, name="Acme"),
            _make_account("v2", "001SF2", health=70.0, nps=7, name="BetaCorp"),
        ]
    )
    slack = FakeSlackSender()
    result = await health_monitor.poll(fake, slack_sender=slack)

    assert result == {"total": 2, "matched": 2, "drops": 0}

    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT account_id, score, nps_category FROM cs_account_health ORDER BY account_id")
        ).mappings().all()
    assert len(rows) == 2
    assert rows[0]["account_id"] == "001SF1"
    assert rows[0]["score"] == 85.0
    assert rows[0]["nps_category"] == "promoter"
    assert rows[1]["nps_category"] == "passive"


@pytest.mark.asyncio
async def test_poll_logs_uid_miss_when_external_id_absent():
    _clear()
    fake = FakeVitally(
        [
            _make_account("v1", "001SF1"),
            _make_account("v_orphan", None, name="Orphan Inc"),
        ]
    )
    result = await health_monitor.poll(fake, slack_sender=FakeSlackSender())

    assert result["total"] == 2
    assert result["matched"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT title FROM tasks WHERE source = 'cs:uid_resolver:v_orphan'")
        ).mappings().first()
    assert task is not None
    assert "Orphan Inc" in task["title"]


@pytest.mark.asyncio
async def test_poll_detects_drop_and_alerts():
    _clear()
    now = datetime.now(timezone.utc)
    engine = get_engine()
    # Seed 5d-ago history with score=90 to establish a peak
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health_history (account_id, score, nps_score, checked_at)
                   VALUES ('001SF1', 90.0, 9, :t)"""
            ),
            {"t": now - timedelta(days=5)},
        )

    fake = FakeVitally([_make_account("v1", "001SF1", health=75.0, nps=9)])
    slack = FakeSlackSender()
    result = await health_monitor.poll(fake, slack_sender=slack, now=now)

    assert result["drops"] == 1
    assert len(slack.sent) == 1
    channel, body = slack.sent[0]
    assert channel == "#agent-cs-log"
    assert "Vitally health drop" in body
    assert "15 pts" in body  # 90 - 75


@pytest.mark.asyncio
async def test_poll_no_alert_below_drop_threshold():
    _clear()
    now = datetime.now(timezone.utc)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health_history (account_id, score, nps_score, checked_at)
                   VALUES ('001SF1', 85.0, 9, :t)"""
            ),
            {"t": now - timedelta(days=3)},
        )

    fake = FakeVitally([_make_account("v1", "001SF1", health=78.0, nps=9)])
    slack = FakeSlackSender()
    result = await health_monitor.poll(fake, slack_sender=slack, now=now)

    assert result["drops"] == 0
    assert len(slack.sent) == 0  # drop of 7 pts < 10 threshold


@pytest.mark.asyncio
async def test_poll_ignores_history_outside_7_day_window():
    _clear()
    now = datetime.now(timezone.utc)
    engine = get_engine()
    # Old history (10d ago) should NOT trigger a drop
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health_history (account_id, score, nps_score, checked_at)
                   VALUES ('001SF1', 95.0, 9, :t)"""
            ),
            {"t": now - timedelta(days=10)},
        )

    fake = FakeVitally([_make_account("v1", "001SF1", health=75.0, nps=9)])
    slack = FakeSlackSender()
    result = await health_monitor.poll(fake, slack_sender=slack, now=now)

    assert result["drops"] == 0
    assert len(slack.sent) == 0


@pytest.mark.asyncio
async def test_poll_appends_history_row():
    _clear()
    fake = FakeVitally([_make_account("v1", "001SF1", health=80.0)])
    await health_monitor.poll(fake, slack_sender=FakeSlackSender())

    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM cs_account_health_history WHERE account_id = '001SF1'")
        ).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_poll_upsert_replaces_current_but_appends_history():
    _clear()
    fake1 = FakeVitally([_make_account("v1", "001SF1", health=80.0)])
    await health_monitor.poll(fake1, slack_sender=FakeSlackSender())
    fake2 = FakeVitally([_make_account("v1", "001SF1", health=82.0)])
    await health_monitor.poll(fake2, slack_sender=FakeSlackSender())

    engine = get_engine()
    with engine.begin() as conn:
        current_count = conn.execute(
            text("SELECT COUNT(*) FROM cs_account_health WHERE account_id = '001SF1'")
        ).scalar()
        history_count = conn.execute(
            text("SELECT COUNT(*) FROM cs_account_health_history WHERE account_id = '001SF1'")
        ).scalar()
        current_score = conn.execute(
            text("SELECT score FROM cs_account_health WHERE account_id = '001SF1'")
        ).scalar()

    assert current_count == 1
    assert history_count == 2
    assert current_score == 82.0
