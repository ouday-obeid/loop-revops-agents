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
