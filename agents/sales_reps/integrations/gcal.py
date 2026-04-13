"""Google Calendar client — service-account read-only.

Powers the pre-demo trigger by listing events on the shared GTM calendar
~2h before start. Service-account credentials are resolved from
`GOOGLE_SERVICE_ACCOUNT_JSON` (path to a JSON key) or `_INLINE` (inline
JSON string). The target calendar is `GCAL_DEMO_CALENDAR_ID`, falling
back to "primary".

Using the googleapiclient library here — `from_service_account_*` is
imported lazily so the rest of `sales_reps/` stays importable when the
Google Python SDK isn't installed locally.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.secrets import get_config, require_secret

log = logging.getLogger(__name__)

_SCOPES = ("https://www.googleapis.com/auth/calendar.readonly",)


class GCalError(RuntimeError):
    pass


def _load_service_account_info() -> dict[str, Any]:
    inline = get_config("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE")
    if inline:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as e:
            raise GCalError("GOOGLE_SERVICE_ACCOUNT_JSON_INLINE is not valid JSON") from e
    path = require_secret("GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise GCalError(f"service-account file not found: {path}") from e


def _service():  # pragma: no cover — thin wrapper, covered via integration test
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GCalError(
            "google-api-python-client + google-auth required; "
            "pip install google-api-python-client google-auth"
        ) from e

    info = _load_service_account_info()
    subject = get_config("GCAL_IMPERSONATE_USER")  # domain-wide delegation target
    creds = service_account.Credentials.from_service_account_info(info, scopes=list(_SCOPES))
    if subject:
        creds = creds.with_subject(subject)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_events(
    *,
    time_min: datetime,
    time_max: datetime,
    calendar_id: str | None = None,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    """Raw calendar events between time_min and time_max.

    Timezones must be UTC; serialize to RFC3339 with Z suffix for the API.
    """
    cal = calendar_id or get_config("GCAL_DEMO_CALENDAR_ID") or "primary"
    svc = _service()
    events = svc.events().list(
        calendarId=cal,
        timeMin=_rfc3339(time_min),
        timeMax=_rfc3339(time_max),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()
    return [_normalize_event(e) for e in events.get("items", [])]


def list_upcoming(
    lookahead_minutes: int = 120,
    *,
    min_lookahead_minutes: int = 90,
    calendar_id: str | None = None,
) -> list[dict[str, Any]]:
    """Events whose start is between now+min_lookahead and now+lookahead.

    Default 90-120 min window — trigger fires once, won't re-fire as the
    meeting gets closer because the window slides forward.
    """
    now = datetime.now(timezone.utc)
    return list_events(
        time_min=now + timedelta(minutes=min_lookahead_minutes),
        time_max=now + timedelta(minutes=lookahead_minutes),
        calendar_id=calendar_id,
    )


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten gcal event object for downstream consumers."""
    start = raw.get("start") or {}
    end = raw.get("end") or {}
    organizer = raw.get("organizer") or {}
    attendees_raw = raw.get("attendees") or []
    attendees = [
        {
            "email": (a.get("email") or "").lower(),
            "name": a.get("displayName"),
            "response": a.get("responseStatus"),
            "organizer": bool(a.get("organizer")),
        }
        for a in attendees_raw if a.get("email")
    ]
    return {
        "id": raw.get("id"),
        "title": raw.get("summary", ""),
        "description": raw.get("description", ""),
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "organizer_email": (organizer.get("email") or "").lower() or None,
        "attendees": attendees,
        "conference_link": (raw.get("hangoutLink")
                            or _find_conf_link(raw.get("conferenceData") or {})),
        "location": raw.get("location"),
    }


def _find_conf_link(cdata: dict[str, Any]) -> str | None:
    for ep in cdata.get("entryPoints") or []:
        if ep.get("entryPointType") == "video":
            return ep.get("uri")
    return None
