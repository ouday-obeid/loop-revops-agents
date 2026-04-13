"""Momentum↔SF sync monitor — diff engine, grace window, alert suppression, degradation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text

from agents.sales_reps import momentum_sync_monitor as msm
from agents.sales_reps import rate_gates
from shared.db.connection import get_engine


def _reset_bucket(bucket: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE bucket = :b"), {"b": bucket})


def _iso_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _call(
    *,
    call_id: str = "CALL_1",
    minutes_ago: int = 30,
    sf_synced: bool | None = True,
    rep_email: str = "ae@tryloop.ai",
    contact_email: str = "buyer@acme.com",
    duration: int = 120,
) -> dict:
    return {
        "id": call_id,
        "started_at": _iso_minutes_ago(minutes_ago),
        "duration_seconds": duration,
        "direction": "outbound",
        "rep_email": rep_email,
        "contact_email": contact_email,
        "sf_synced": sf_synced,
        "sf_task_id": "00T1" if sf_synced else None,
    }


# --------------------------------------------------------------- helpers

def test_parse_iso_handles_z_suffix():
    dt = msm._parse_iso("2026-04-13T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_iso_returns_none_for_junk():
    assert msm._parse_iso("") is None
    assert msm._parse_iso("not a date") is None
    assert msm._parse_iso(None) is None


def test_within_grace_rejects_fresh_calls():
    # A call 2 minutes ago is NOT past the 15-min grace window.
    assert msm._within_grace(_iso_minutes_ago(2), 15) is False


def test_within_grace_accepts_old_calls():
    # A call 30 minutes ago IS past the 15-min grace window.
    assert msm._within_grace(_iso_minutes_ago(30), 15) is True


def test_within_grace_rejects_missing_timestamp():
    assert msm._within_grace(None, 15) is False


# --------------------------------------------------------------- SF probe

def test_find_sf_task_matches_on_callobject():
    hit = {"records": [{"Id": "00T1", "CallObject": "CALL_A"}]}
    call = _call(call_id="CALL_A")
    with patch.object(msm.salesforce_mcp, "soql_query", return_value=hit):
        out = msm._find_sf_task(call)
    assert out is not None
    assert out["Id"] == "00T1"


def test_find_sf_task_falls_back_to_time_window():
    # Preferred CallObject lookup misses; time-window probe hits.
    miss = {"records": []}
    hit = {"records": [{"Id": "00T2", "CallObject": None}]}
    call = _call(call_id="CALL_B")
    with patch.object(msm.salesforce_mcp, "soql_query", side_effect=[miss, hit]):
        out = msm._find_sf_task(call)
    assert out is not None
    assert out["Id"] == "00T2"


def test_find_sf_task_returns_none_when_both_probes_miss():
    empty = {"records": []}
    call = _call(call_id="CALL_C")
    with patch.object(msm.salesforce_mcp, "soql_query", side_effect=[empty, empty]):
        out = msm._find_sf_task(call)
    assert out is None


def test_find_sf_task_skips_time_probe_without_rep_email():
    empty = {"records": []}
    call = _call(call_id="CALL_D")
    call["rep_email"] = None
    with patch.object(msm.salesforce_mcp, "soql_query", return_value=empty) as m:
        out = msm._find_sf_task(call)
    assert out is None
    # Only the CallObject probe should run when rep_email missing.
    assert m.call_count == 1


def test_find_sf_task_degrades_on_probe_exception():
    call = _call(call_id="CALL_E")
    with patch.object(msm.salesforce_mcp, "soql_query",
                      side_effect=RuntimeError("SOQL down")):
        out = msm._find_sf_task(call)
    assert out is None


# --------------------------------------------------------------- break detection

def test_detect_breaks_flags_sf_synced_false():
    calls = [_call(call_id="CALL_1", sf_synced=False, minutes_ago=5)]
    # Even though minutes_ago=5 is inside grace, sf_synced=False is trusted immediately.
    out = msm._detect_breaks(calls)
    assert len(out) == 1
    assert out[0].reason == "sf_synced_false"


def test_detect_breaks_respects_grace_window():
    # A call 2 min old with sf_synced=True not yet checkable — skip.
    calls = [_call(call_id="CALL_2", sf_synced=True, minutes_ago=2)]
    with patch.object(msm, "_find_sf_task", return_value=None):
        out = msm._detect_breaks(calls)
    assert out == []


def test_detect_breaks_flags_no_sf_task():
    calls = [_call(call_id="CALL_3", sf_synced=True, minutes_ago=30)]
    with patch.object(msm, "_find_sf_task", return_value=None):
        out = msm._detect_breaks(calls)
    assert len(out) == 1
    assert out[0].reason == "no_sf_task_found"
    assert out[0].momentum_call_id == "CALL_3"


def test_detect_breaks_clean_when_sf_task_found():
    calls = [_call(call_id="CALL_4", sf_synced=True, minutes_ago=30)]
    with patch.object(msm, "_find_sf_task", return_value={"Id": "00T9"}):
        out = msm._detect_breaks(calls)
    assert out == []


def test_detect_breaks_mixed():
    calls = [
        _call(call_id="BROKEN", sf_synced=False, minutes_ago=5),
        _call(call_id="FRESH", sf_synced=True, minutes_ago=2),     # skipped (grace)
        _call(call_id="OLD_OK", sf_synced=True, minutes_ago=30),    # task found
        _call(call_id="OLD_MISSING", sf_synced=True, minutes_ago=30),  # missing
    ]
    def probe(call):
        return {"Id": "00T"} if call["id"] == "OLD_OK" else None
    with patch.object(msm, "_find_sf_task", side_effect=probe):
        out = msm._detect_breaks(calls)
    ids = {b.momentum_call_id for b in out}
    assert ids == {"BROKEN", "OLD_MISSING"}


# --------------------------------------------------------------- rendering

def test_render_slack_clean_report():
    out = msm._render_slack(50, [], alert_suppressed=False)
    assert "✓" in out
    assert "50 calls checked" in out


def test_render_slack_break_report_groups_by_rep():
    breaks = [
        msm.SyncBreak("C1", "2026-04-13T12:00:00Z", "a@x.com", "b@y.com", 60, "no_sf_task_found"),
        msm.SyncBreak("C2", "2026-04-13T12:30:00Z", "a@x.com", "c@y.com", 90, "sf_synced_false"),
        msm.SyncBreak("C3", "2026-04-13T12:45:00Z", "d@x.com", "e@y.com", 30, "sf_synced_false"),
    ]
    out = msm._render_slack(10, breaks, alert_suppressed=False)
    assert "3 call(s) missing" in out
    assert "a@x.com=2" in out
    assert "d@x.com=1" in out


def test_render_slack_notes_suppression():
    breaks = [msm.SyncBreak("C1", "2026-04-13T12:00:00Z", "a@x.com", "b@y.com", 60, "sf_synced_false")]
    out = msm._render_slack(5, breaks, alert_suppressed=True)
    assert "suppressed" in out.lower()


def test_render_slack_truncates_above_10():
    breaks = [
        msm.SyncBreak(f"C{i}", "2026-04-13T12:00:00Z", f"r{i}@x.com",
                      "b@y.com", 30, "sf_synced_false")
        for i in range(15)
    ]
    out = msm._render_slack(20, breaks, alert_suppressed=False)
    assert "…and 5 more" in out


# --------------------------------------------------------------- run_once

def test_run_once_clean_does_not_consume_rate_gate():
    _reset_bucket("sales_reps_sync_alert_hourly")
    fake_calls = [_call(call_id="OK", sf_synced=True, minutes_ago=30)]
    with patch.object(msm.momentum, "list_recent_calls", return_value=fake_calls), \
         patch.object(msm, "_find_sf_task", return_value={"Id": "00T"}):
        out = asyncio.run(msm.run_once())
    assert out["breaks"] == []
    assert out["calls_checked"] == 1
    # No alert → bucket should be untouched.
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT count FROM rate_limits WHERE bucket = :b"),
            {"b": "sales_reps_sync_alert_hourly"},
        ).fetchone()
    assert row is None


def test_run_once_flags_and_reports_break():
    _reset_bucket("sales_reps_sync_alert_hourly")
    fake_calls = [_call(call_id="MISSING", sf_synced=False, minutes_ago=10)]
    with patch.object(msm.momentum, "list_recent_calls", return_value=fake_calls):
        out = asyncio.run(msm.run_once())
    assert len(out["breaks"]) == 1
    assert out["breaks"][0]["reason"] == "sf_synced_false"
    assert "SYNC BREAK" in out["text"]
    assert out["alert_suppressed"] is False


def test_run_once_suppresses_alert_on_second_break_within_hour(monkeypatch):
    _reset_bucket("sales_reps_sync_alert_hourly")
    # Drop the per-hour limit to 1 so the second run is over.
    monkeypatch.setitem(rate_gates._LIMITS, "sales_reps_sync_alert_hourly", 1)
    fake_calls = [_call(call_id="MISSING", sf_synced=False, minutes_ago=10)]
    with patch.object(msm.momentum, "list_recent_calls", return_value=fake_calls):
        first = asyncio.run(msm.run_once())
        second = asyncio.run(msm.run_once())
    assert first["alert_suppressed"] is False
    assert second["alert_suppressed"] is True
    # Both runs still return the break list — only the alert is suppressed.
    assert len(second["breaks"]) == 1


def test_run_once_degrades_when_momentum_down():
    with patch.object(msm.momentum, "list_recent_calls",
                      side_effect=RuntimeError("connection refused")):
        out = asyncio.run(msm.run_once())
    assert "unreachable" in out["text"].lower()
    assert out["breaks"] == []
    assert out["calls_checked"] == 0
    assert "error" in out


def test_run_once_writes_audit_row():
    _reset_bucket("sales_reps_sync_alert_hourly")
    fake_calls = [_call(call_id="OK", sf_synced=True, minutes_ago=30)]
    with patch.object(msm.momentum, "list_recent_calls", return_value=fake_calls), \
         patch.object(msm, "_find_sf_task", return_value={"Id": "00T"}):
        asyncio.run(msm.run_once())
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT agent_name, action FROM audit_log "
                "WHERE action = 'sales_reps_sync_check' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "sales_reps"


@pytest.mark.parametrize("minutes,expected", [(2, False), (20, True)])
def test_within_grace_parametrized(minutes, expected):
    assert msm._within_grace(_iso_minutes_ago(minutes), 15) is expected
