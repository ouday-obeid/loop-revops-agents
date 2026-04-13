"""Tests for stall-alert Slack button handlers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

from agents.onboarding import milestone_monitor as mm
from agents.onboarding import stall_actions
from shared.db.connection import get_engine


def _body(action_id: str, onboarding_id: str, user: str = "U_JACKIE") -> dict:
    return {
        "actions": [{"action_id": action_id, "value": onboarding_id}],
        "user": {"id": user},
        "channel": {"id": "D_JACKIE"},
    }


# ---------- business-day math ----------

def test_advance_business_days_skips_weekends():
    # 2026-04-10 is a Friday → +3 business days should land on Wednesday 04-15.
    friday = datetime(2026, 4, 10, 12, 0, tzinfo=timezone.utc)
    out = stall_actions._advance_business_days(friday, 3)
    assert out.date().isoformat() == "2026-04-15"
    assert out.tzinfo is not None


def test_advance_business_days_same_week_when_starting_monday():
    # 2026-04-13 is a Monday → +3 bdays = Thursday 04-16.
    monday = datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc)
    out = stall_actions._advance_business_days(monday, 3)
    assert out.date().isoformat() == "2026-04-16"


# ---------- extend handler ----------

@pytest.mark.asyncio
async def test_handle_extend_3d_silences_alert_window():
    onboarding_id = "a01STALLABC"
    mm._ensure_dedup_table()
    # Pretend we alerted an hour ago so _recently_alerted is True.
    mm._record_alert(onboarding_id, "jk=x;overall=y")
    assert mm._recently_alerted(onboarding_id, "jk=x;overall=y") is True

    result = await stall_actions.handle_extend_3d(_body("stall_extend_3d", onboarding_id))
    assert "Snoozed" in result["text"] or "snooze" in result["text"].lower()
    assert onboarding_id in result["text"]

    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT last_alerted_at FROM onboarding_stall_alerts "
                "WHERE onboarding_id = :id"
            ),
            {"id": onboarding_id},
        ).fetchone()
    assert row is not None
    last = row[0]
    if isinstance(last, str):
        last = datetime.fromisoformat(last.replace("Z", "+00:00"))
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    # Pushed at least 3 calendar days forward (3 business days ≥ 3 calendar days).
    assert last - datetime.now(timezone.utc) >= timedelta(days=2, hours=12)


@pytest.mark.asyncio
async def test_handle_extend_3d_writes_audit_row():
    onboarding_id = "a01STALLAUD"
    result = await stall_actions.handle_extend_3d(
        _body("stall_extend_3d", onboarding_id, user="U_OUDAY")
    )
    assert onboarding_id in result["text"]

    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT agent_name, action, target, after_value "
                "FROM audit_log WHERE action='stall_extended' "
                "ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "onboarding"
    assert row[1] == "stall_extended"
    assert onboarding_id in row[2]
    assert "U_OUDAY" in row[3]


@pytest.mark.asyncio
async def test_handle_extend_3d_rejects_missing_onboarding_id():
    body = {"actions": [], "user": {"id": "U_JACKIE"}, "channel": {"id": "D_JACKIE"}}
    result = await stall_actions.handle_extend_3d(body)
    assert "Missing" in result["text"]


# ---------- escalate handler ----------

@pytest.mark.asyncio
async def test_handle_escalate_posts_to_jackie_channel(monkeypatch):
    sent: list[tuple[str, str]] = []

    class FakeSender:
        def send(self, channel, text_, blocks=None):
            sent.append((channel, text_))
            return {"ok": True, "ts": "1", "channel": channel}

    from shared import slack_dispatcher as sd
    monkeypatch.setattr(sd, "SlackSender", lambda: FakeSender())
    monkeypatch.setenv("ONBOARDING_JACKIE_CHANNEL", "#cs-oncall")
    monkeypatch.setenv("ONBOARDING_O_DM", "D_OUDAY")

    onboarding_id = "a01STALLESC"
    result = await stall_actions.handle_escalate(
        _body("stall_escalate", onboarding_id, user="U_JACKIE")
    )
    assert "Escalated" in result["text"]
    assert onboarding_id in result["text"]

    channels = [c for c, _ in sent]
    assert "#cs-oncall" in channels
    assert "D_OUDAY" in channels
    for _, text_ in sent:
        assert onboarding_id in text_
        assert "U_JACKIE" in text_


@pytest.mark.asyncio
async def test_handle_escalate_writes_audit_row(monkeypatch):
    class FakeSender:
        def send(self, channel, text_, blocks=None):
            return {"ok": True}

    from shared import slack_dispatcher as sd
    monkeypatch.setattr(sd, "SlackSender", lambda: FakeSender())

    onboarding_id = "a01STALLESCA"
    await stall_actions.handle_escalate(
        _body("stall_escalate", onboarding_id, user="U_JACKIE")
    )
    with get_engine().begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT action, target, after_value FROM audit_log "
                "WHERE action='stall_escalated' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "stall_escalated"
    assert onboarding_id in row[1]
    assert "U_JACKIE" in row[2]


@pytest.mark.asyncio
async def test_handle_escalate_skips_o_dm_when_not_configured(monkeypatch):
    sent: list[str] = []

    class FakeSender:
        def send(self, channel, text_, blocks=None):
            sent.append(channel)
            return {"ok": True}

    from shared import slack_dispatcher as sd
    monkeypatch.setattr(sd, "SlackSender", lambda: FakeSender())
    monkeypatch.setenv("ONBOARDING_JACKIE_CHANNEL", "#jackie-only")
    monkeypatch.delenv("ONBOARDING_O_DM", raising=False)

    await stall_actions.handle_escalate(_body("stall_escalate", "a01STALLNOO"))
    assert sent == ["#jackie-only"]


# ---------- bootstrap wiring ----------

def test_bootstrap_registers_action_handlers():
    from agents.onboarding import main as ob_main
    from shared import slack_dispatcher as sd

    ob_main.bootstrap()
    assert "stall_extend_3d" in sd._action_handlers
    assert "stall_escalate" in sd._action_handlers
    assert sd._action_handlers["stall_extend_3d"] is stall_actions.handle_extend_3d
    assert sd._action_handlers["stall_escalate"] is stall_actions.handle_escalate
