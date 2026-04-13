"""Fireflies adapter — normalizes raw transcripts for grading.

Thin wrapper around shared.mcp.fireflies_mcp. Adds the sales-specific filtering
and shape normalization the grader needs:

  - Single `NormalizedTranscript` dataclass
  - Rep identification: host_email first, fallback to first internal-domain participant
  - Attendee split: internal vs external by email domain
  - Full-text rendering: "<speaker>: <text>" per line
  - Token-budget sampling: head + tail sampling if transcript is too long
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from shared.mcp import fireflies_mcp
from shared.secrets import get_config

log = logging.getLogger(__name__)

# Internal email domain for Loop AI. Configurable via env for testing.
_DEFAULT_INTERNAL_DOMAIN = "tryloop.ai"

# Token budget for grading input (rough: ~4 chars per token).
_MAX_TRANSCRIPT_CHARS = 200_000  # ≈ 50K tokens


@dataclass
class NormalizedTranscript:
    meeting_id: str
    title: str
    date: str | None
    duration_minutes: float | None
    host_email: str | None
    rep_email: str | None
    rep_name: str | None
    internal_attendees: list[str] = field(default_factory=list)
    external_attendees: list[str] = field(default_factory=list)
    sentences: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    transcript_url: str | None = None

    @property
    def has_external_attendees(self) -> bool:
        return len(self.external_attendees) > 0

    def rendered_text(self, max_chars: int = _MAX_TRANSCRIPT_CHARS) -> str:
        """Render sentences as `Speaker: text` lines with head+tail sampling."""
        lines = [
            f"{s.get('speaker_name') or 'Unknown'}: {s.get('text') or ''}"
            for s in self.sentences if s.get("text")
        ]
        joined = "\n".join(lines)
        if len(joined) <= max_chars:
            return joined
        # Keep first 60% and last 30% of budget; mark the gap so the LLM knows.
        head_budget = int(max_chars * 0.6)
        tail_budget = int(max_chars * 0.3)
        head = joined[:head_budget]
        tail = joined[-tail_budget:]
        return head + "\n\n[... transcript truncated for length ...]\n\n" + tail


def _internal_domain() -> str:
    return get_config("SALES_REPS_INTERNAL_DOMAIN", _DEFAULT_INTERNAL_DOMAIN) or _DEFAULT_INTERNAL_DOMAIN


def _split_attendees(emails: list[str]) -> tuple[list[str], list[str]]:
    """Returns (internal, external)."""
    domain = _internal_domain().lower()
    internal, external = [], []
    for e in emails:
        if not e:
            continue
        e = e.strip().lower()
        if not e:
            continue
        (internal if e.endswith(f"@{domain}") else external).append(e)
    return internal, external


def _pick_rep(host_email: str | None, internal: list[str]) -> str | None:
    """The rep is the host if internal; otherwise the first internal attendee."""
    if host_email and internal and host_email.lower() in internal:
        return host_email.lower()
    if internal:
        return internal[0]
    return host_email.lower() if host_email else None


def normalize(raw: dict[str, Any]) -> NormalizedTranscript:
    """Normalize a Fireflies GraphQL response into our adapter shape."""
    participants = raw.get("participants") or []
    if isinstance(participants, str):
        participants = [p.strip() for p in participants.split(",") if p.strip()]
    internal, external = _split_attendees([*participants])

    host_email = raw.get("host_email")
    rep_email = _pick_rep(host_email, internal)
    rep_name = None
    # Try to derive rep name from first sentence by a speaker matching rep_email's local-part.
    sentences = raw.get("sentences") or []
    if rep_email and sentences:
        local = rep_email.split("@")[0].lower()
        for s in sentences:
            name = (s.get("speaker_name") or "").strip()
            if name and local in name.lower().replace(" ", ""):
                rep_name = name
                break

    duration = raw.get("duration")
    if isinstance(duration, (int, float)) and duration > 600:
        # Some Fireflies responses return seconds; normalize to minutes when it looks like seconds.
        duration_min = duration / 60.0
    elif isinstance(duration, (int, float)):
        duration_min = float(duration)
    else:
        duration_min = None

    return NormalizedTranscript(
        meeting_id=raw["id"],
        title=raw.get("title") or "",
        date=raw.get("date"),
        duration_minutes=duration_min,
        host_email=(host_email or "").lower() or None,
        rep_email=rep_email,
        rep_name=rep_name,
        internal_attendees=internal,
        external_attendees=external,
        sentences=list(sentences),
        summary=raw.get("summary") or {},
        transcript_url=raw.get("transcript_url"),
    )


def fetch_and_normalize(meeting_id: str) -> NormalizedTranscript:
    raw = fireflies_mcp.get_transcript(meeting_id)
    if not raw:
        raise ValueError(f"Fireflies returned no transcript for meeting_id={meeting_id}")
    return normalize(raw)


def list_recent(from_date: str | None = None, to_date: str | None = None,
                limit: int = 50) -> list[dict[str, Any]]:
    """Light list for batch/polling; returns Fireflies' summary rows unchanged."""
    return fireflies_mcp.list_transcripts(from_date=from_date, to_date=to_date, limit=limit)
