"""Cover briefings, board_monitor, integration_health with mocked dependencies."""
import asyncio
from unittest.mock import MagicMock, patch

from sqlalchemy import text

from shared.db.connection import get_engine


def _seed(title="seed task", priority="high"):
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, status, priority, category, source)
                   VALUES ('oo', :t, 'pending', :p, 'data_quality', :s)"""
            ),
            {"t": title, "p": priority, "s": f"seed:{title}"},
        )


def test_briefings_compose_and_send():
    from agents.oo import briefings
    _seed("daily-brief test")
    msg = briefings._compose_daily()
    assert "Daily briefing" in msg

    weekly = briefings._compose_weekly()
    assert "Weekly Review" in weekly

    mock_sender = MagicMock()
    mock_sender.send.return_value = {"ok": True, "ts": "1.2"}
    result = asyncio.run(briefings.send_daily_briefing(mock_sender))
    assert result["ok"] is True
    mock_sender.send.assert_called_once()


# --------------------------- Tier 7: briefings urgent + tokens + OOO

def test_daily_briefing_includes_urgent_overnight_section():
    """Tasks marked priority='urgent' OR category in {urgent_fire,
    automation_broken, integration_broken} created in the last 16h
    appear in the new Urgent overnight section."""
    from agents.oo import briefings
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, status, priority,
                                      category, source)
                   VALUES ('oo', :t, 'pending', 'urgent', 'urgent_fire', :s)"""
            ),
            {"t": "MARKER overnight urgent task", "s": "test:urgent_overnight_marker"},
        )
    msg = briefings._compose_daily()
    assert "Urgent overnight" in msg
    assert "MARKER overnight urgent task" in msg


def test_weekly_review_shows_tokens_sum():
    """SUM(tokens_used) FROM agent_runs appears alongside Claude spend."""
    from agents.oo import briefings
    with get_engine().begin() as conn:
        conn.execute(
            text(
                """INSERT INTO agent_runs (agent_name, trigger, status, tokens_used,
                                            cost_usd, started_at)
                   VALUES ('test', 'unit', 'success', 12345, 0.50,
                           datetime('now', '-1 days'))"""
            )
        )
    msg = briefings._compose_weekly()
    assert "Tokens:" in msg
    assert "12,345" in msg or "12345" in msg


def test_briefing_skipped_when_ooo_event_active(monkeypatch):
    """When _is_user_ooo_now() returns True, _send returns skipped=ooo
    instead of calling sender.send()."""
    from agents.oo import briefings

    monkeypatch.setattr(briefings, "_is_user_ooo_now", lambda: True)
    sender_called = []

    class _S:
        def send(self, channel, text_, blocks=None):
            sender_called.append({"channel": channel, "text": text_})
            return {"ok": True}

    result = asyncio.run(briefings._send("test message", _S()))
    assert result.get("skipped") == "ooo"
    assert sender_called == []


def test_briefing_proceeds_when_no_ooo_event(monkeypatch):
    from agents.oo import briefings

    monkeypatch.setattr(briefings, "_is_user_ooo_now", lambda: False)

    class _S:
        def __init__(self): self.sent = []
        def send(self, channel, text_, blocks=None):
            self.sent.append((channel, text_))
            return {"ok": True}

    s = _S()
    result = asyncio.run(briefings._send("hi O", s))
    assert result.get("ok") is True
    assert len(s.sent) == 1


def test_ooo_check_returns_false_without_creds(monkeypatch):
    """No GOOGLE_SERVICE_ACCOUNT_JSON / _INLINE → not OOO (proceed with send).
    Avoids silencing the briefing on a missing-config error."""
    from agents.oo import briefings
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE", raising=False)
    assert briefings._is_user_ooo_now() is False


def test_ooo_check_matches_keywords_in_event_title(monkeypatch):
    """Real positive case: GCal returns one event with 'OOO' in title; we
    detect it as an active OOO event."""
    from agents.oo import briefings
    from agents.sales_reps.integrations import gcal

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE", '{"fake":"creds"}')
    monkeypatch.setattr(
        gcal, "list_events",
        lambda **kw: [{"id": "x", "title": "OOO — Easter Monday", "description": ""}],
    )
    assert briefings._is_user_ooo_now() is True


def test_ooo_check_ignores_non_ooo_event(monkeypatch):
    from agents.oo import briefings
    from agents.sales_reps.integrations import gcal

    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE", '{"fake":"creds"}')
    monkeypatch.setattr(
        gcal, "list_events",
        lambda **kw: [{"id": "x", "title": "weekly forecast review", "description": ""}],
    )
    assert briefings._is_user_ooo_now() is False


def test_board_monitor_with_mock_slack():
    from agents.oo import board_monitor
    mock_slack = MagicMock()
    mock_slack.conversations_history.return_value = {
        "messages": [
            {"ts": "9999.1", "text": "URGENT the flow is broken", "user": "U1"},
            {"ts": "9999.2", "text": "just chatting", "user": "U2"},
        ]
    }
    created = asyncio.run(board_monitor.scan_slack(mock_slack))
    # First message should classify as urgent/automation; second as 'other' and be skipped
    assert any(c.get("alert") for c in created) or any(c for c in created)


def test_board_monitor_fireflies_with_mock():
    from agents.oo import board_monitor
    ff = MagicMock()
    ff.list_transcripts.return_value = [
        {"id": "FFABC", "title": "pipeline hygiene sync — stale opp", "summary": {"overview": ""}}
    ]
    created = asyncio.run(board_monitor.scan_fireflies(ff))
    assert len(created) == 1
    assert created[0]["category"] == "pipeline_hygiene"


def test_board_monitor_scan_entrypoint_nocrash():
    from agents.oo import board_monitor
    # With no clients, it should simply return zeros.
    result = asyncio.run(board_monitor.scan())
    assert result == {"slack": 0, "fireflies": 0}


# --------------------------- Tier 6: alertworthy DM alerts (parent 11736893953)


class _SenderCapture:
    """Stand-in for SlackSender that records every send() call."""

    def __init__(self, client=None):
        self.sent: list[dict] = []

    def send(self, channel, text_, blocks=None):
        self.sent.append({"channel": channel, "text": text_})
        return {"ok": True, "ts": f"{len(self.sent)}.0", "channel": channel}


import pytest  # noqa: E402 — fixture is local to this section


@pytest.fixture
def _wipe_oo_tasks_after():
    """Clean up oo-created tasks so later tests (e.g. test_board_summary_
    with_seeded_task in test_dispatcher.py) see a quiet board. The board
    monitor tests insert many urgent tasks that would otherwise crowd CEO
    out of the LIMIT 10 board summary."""
    yield
    with get_engine().begin() as conn:
        conn.execute(
            text("DELETE FROM tasks WHERE agent_name = 'oo' AND source LIKE 'slack:%'")
        )
        conn.execute(
            text("DELETE FROM tasks WHERE agent_name = 'oo' AND source LIKE 'fireflies:%'")
        )


def _single_channel_slack(message_text: str, ts: str):
    """Build a slack mock that returns the given message ONLY for #revops
    and empty for the other 4 CHANNELS — keeps the test from inserting 5
    duplicate tasks (one per scanned channel)."""
    mock = MagicMock()

    def _hist(channel, limit):
        if channel == "#revops":
            return {"messages": [{"ts": ts, "text": message_text, "user": "U_X"}]}
        return {"messages": []}

    mock.conversations_history.side_effect = _hist
    return mock


def test_urgent_fire_in_slack_message_triggers_o_dm(_wipe_oo_tasks_after):
    from agents.oo import board_monitor
    capture = _SenderCapture()
    mock_slack = _single_channel_slack(
        "URGENT — sf write API down, blocking all releases", "1101.1"
    )
    asyncio.run(board_monitor.scan_slack(mock_slack, sender=capture))
    assert len(capture.sent) == 1
    assert "urgent_fire" in capture.sent[0]["text"]


def test_automation_broken_in_slack_message_triggers_o_dm(_wipe_oo_tasks_after):
    from agents.oo import board_monitor
    capture = _SenderCapture()
    mock_slack = _single_channel_slack(
        "the renewal flow is broken since this morning", "1102.1"
    )
    asyncio.run(board_monitor.scan_slack(mock_slack, sender=capture))
    assert len(capture.sent) == 1
    assert "automation_broken" in capture.sent[0]["text"]


def test_data_quality_100pct_hidden_triggers_o_dm(_wipe_oo_tasks_after):
    """100%-hidden Momentum activity is the canonical 'silent break' that
    parent 11736893953 calls out as needing immediate DM escalation."""
    from agents.oo import board_monitor
    capture = _SenderCapture()
    mock_slack = _single_channel_slack(
        "noticed our Momentum activity is 100% hidden again", "1103.1"
    )
    asyncio.run(board_monitor.scan_slack(mock_slack, sender=capture))
    assert len(capture.sent) == 1
    assert "data_quality" in capture.sent[0]["text"]


def test_non_alertworthy_classification_does_not_dm(_wipe_oo_tasks_after):
    """Pipeline_hygiene + similar non-alert categories create a task but do
    NOT page O via DM. Otherwise the DM channel would become noise and the
    2-min SLO promise loses its meaning."""
    from agents.oo import board_monitor
    capture = _SenderCapture()
    mock_slack = _single_channel_slack(
        "this opportunity has been stale for 60 days, no next step", "1104.1"
    )
    asyncio.run(board_monitor.scan_slack(mock_slack, sender=capture))
    assert capture.sent == []


def test_fireflies_alertworthy_signal_triggers_o_dm(_wipe_oo_tasks_after):
    from agents.oo import board_monitor
    capture = _SenderCapture()
    ff = MagicMock()
    ff.list_transcripts.return_value = [
        {"id": "FFFIRE", "title": "URGENT call: prod CRM is on fire",
         "summary": {"overview": "discussed sev1"}},
    ]
    asyncio.run(board_monitor.scan_fireflies(ff, sender=capture))
    assert len(capture.sent) >= 1


def test_alert_o_swallows_send_exceptions():
    """A Slack outage must NOT prevent task creation in the same scan loop."""
    from agents.oo import board_monitor

    class _BadSender:
        def __init__(self, client=None): pass
        def send(self, *a, **kw):
            raise RuntimeError("slack down")

    # Should NOT raise.
    board_monitor._alert_o("title", "urgent_fire", "src", "snip", sender=_BadSender())


def test_integration_health_records_status():
    from agents.oo import integration_health

    async def fake_sf(): return ("healthy", None)
    async def fake_ff(): return ("degraded", "no key")
    async def fake_sl(): return ("healthy", None)
    async def fake_mo(): return ("healthy", "skipped")
    async def fake_vi(): return ("degraded", "not configured")

    with patch.object(integration_health, "_check_salesforce", fake_sf), \
         patch.object(integration_health, "_check_fireflies", fake_ff), \
         patch.object(integration_health, "_check_slack", fake_sl), \
         patch.object(integration_health, "_check_momentum", fake_mo), \
         patch.object(integration_health, "_check_vitally", fake_vi):
        result = asyncio.run(integration_health.poll())
    assert result["salesforce"]["status"] == "healthy"
    assert result["fireflies"]["status"] == "degraded"
    with get_engine().begin() as conn:
        rows = conn.execute(
            text("SELECT integration FROM integration_health")
        ).fetchall()
    names = {r[0] for r in rows}
    assert {"salesforce", "fireflies", "slack", "momentum", "vitally"}.issubset(names)


async def _coro(val):
    return val


def _async_ret(val):
    """Return a coroutine that resolves to val — patches an async fn."""
    return _coro(val)
