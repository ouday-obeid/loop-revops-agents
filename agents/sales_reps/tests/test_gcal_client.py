"""Google Calendar client — event normalization, RFC3339, config loading."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.sales_reps.integrations import gcal


def test_rfc3339_formats_utc_with_z():
    dt = datetime(2026, 4, 13, 14, 0, 0, tzinfo=timezone.utc)
    assert gcal._rfc3339(dt) == "2026-04-13T14:00:00Z"


def test_rfc3339_assumes_utc_when_naive():
    dt = datetime(2026, 4, 13, 14, 0, 0)
    assert gcal._rfc3339(dt).endswith("Z")


def test_normalize_event_flattens_gcal_payload():
    raw = {
        "id": "EVT_1",
        "summary": "Loop Demo — Acme",
        "description": "ok",
        "start": {"dateTime": "2026-04-13T16:00:00Z"},
        "end": {"dateTime": "2026-04-13T16:30:00Z"},
        "organizer": {"email": "Ae@tryloop.ai"},
        "attendees": [
            {"email": "AE@tryloop.ai", "responseStatus": "accepted", "organizer": True},
            {"email": "buyer@acme.com", "responseStatus": "accepted", "displayName": "Buyer"},
        ],
        "hangoutLink": "https://meet.google.com/abc",
    }
    out = gcal._normalize_event(raw)
    assert out["id"] == "EVT_1"
    assert out["title"] == "Loop Demo — Acme"
    assert out["organizer_email"] == "ae@tryloop.ai"
    assert len(out["attendees"]) == 2
    assert out["attendees"][1]["email"] == "buyer@acme.com"
    assert out["conference_link"] == "https://meet.google.com/abc"


def test_normalize_event_handles_all_day():
    raw = {"id": "EVT_2", "summary": "OOO", "start": {"date": "2026-04-13"}, "end": {"date": "2026-04-14"}}
    out = gcal._normalize_event(raw)
    assert out["start"] == "2026-04-13"


def test_normalize_event_picks_conference_from_entry_points():
    raw = {
        "id": "EVT_3", "summary": "x", "start": {}, "end": {},
        "conferenceData": {"entryPoints": [
            {"entryPointType": "phone"},
            {"entryPointType": "video", "uri": "https://zoom.us/j/1"},
        ]},
    }
    out = gcal._normalize_event(raw)
    assert out["conference_link"] == "https://zoom.us/j/1"


def test_normalize_event_drops_attendees_without_email():
    raw = {
        "id": "EVT_4", "summary": "x", "start": {}, "end": {},
        "attendees": [{"displayName": "No email"}, {"email": "a@b.com"}],
    }
    out = gcal._normalize_event(raw)
    assert len(out["attendees"]) == 1


def test_load_service_account_info_inline(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE",
                       json.dumps({"type": "service_account", "client_email": "x@y"}))
    info = gcal._load_service_account_info()
    assert info["client_email"] == "x@y"


def test_load_service_account_info_bad_json(monkeypatch):
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE", "{not json")
    with pytest.raises(gcal.GCalError):
        gcal._load_service_account_info()


def test_load_service_account_info_missing_file(monkeypatch, tmp_path):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE", raising=False)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(tmp_path / "does-not-exist.json"))
    with pytest.raises(gcal.GCalError):
        gcal._load_service_account_info()


def test_list_events_calls_api_with_correct_params():
    fake_svc = MagicMock()
    events_list = MagicMock()
    fake_svc.events.return_value = events_list
    events_list.list.return_value.execute.return_value = {"items": [
        {"id": "EVT_1", "summary": "Demo", "start": {"dateTime": "2026-04-13T16:00:00Z"},
         "end": {"dateTime": "2026-04-13T16:30:00Z"}, "attendees": []},
    ]}
    with patch.object(gcal, "_service", return_value=fake_svc):
        out = gcal.list_events(
            time_min=datetime(2026, 4, 13, 14, tzinfo=timezone.utc),
            time_max=datetime(2026, 4, 13, 16, tzinfo=timezone.utc),
            calendar_id="cal_demo",
        )
    assert len(out) == 1
    assert out[0]["id"] == "EVT_1"
    # Verify the call params carried through.
    kwargs = events_list.list.call_args.kwargs
    assert kwargs["calendarId"] == "cal_demo"
    assert kwargs["timeMin"] == "2026-04-13T14:00:00Z"


def test_list_upcoming_uses_lookahead_window():
    with patch.object(gcal, "list_events", return_value=[]) as m:
        gcal.list_upcoming(lookahead_minutes=120, min_lookahead_minutes=90)
    # Called once with time_min/time_max 90-120 min from now.
    args = m.call_args.kwargs
    delta = (args["time_max"] - args["time_min"]).total_seconds()
    assert 29 * 60 <= delta <= 31 * 60  # 30 minute window
