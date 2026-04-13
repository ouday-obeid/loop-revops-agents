"""Call-type classifier — rule cascade + Haiku fallback.

Order of operations (cheapest → most expensive):
  1. No external attendees → internal
  2. Title regex → onboarding / cs / pilot / renewal / headroom / second_call / first_call / follow_up
  3. Duration heuristic for SDR-style calls (short + certain attendees)
  4. Haiku fallback for the ambiguous tail

Returns a `Classification` dataclass with type, confidence, and reason — the
grader uses `type` and logs the rest for auditability.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from shared.secrets import get_config, require_secret

from agents.sales_reps.call_grader.fireflies_adapter import NormalizedTranscript

log = logging.getLogger(__name__)

_HAIKU_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class Classification:
    call_type: str       # one of rubrics.AnyCallType literals
    confidence: float    # 0..1
    reason: str          # brief note: which rule fired

    @property
    def is_gradable(self) -> bool:
        from agents.sales_reps.call_grader import rubrics  # local import to avoid cycle
        return self.call_type in rubrics.GRADABLE_TYPES


# ---------------------------------------------------------------- title patterns

_TITLE_PATTERNS: tuple[tuple[str, re.Pattern[str], float], ...] = (
    ("internal",     re.compile(r"\b(internal|1:1|standup|sync|pipeline review|team meeting)\b", re.I), 0.95),
    ("onboarding",   re.compile(r"\b(onboarding|kickoff|kick-off|launch call|implementation)\b", re.I), 0.9),
    ("cs",           re.compile(r"\b(customer success|qbr|ebr|business review|account review)\b", re.I), 0.9),
    ("pilot",        re.compile(r"\b(pilot|poc|proof of concept|trial check)\b", re.I), 0.9),
    ("renewal",      re.compile(r"\b(renewal|expansion|upsell)\b", re.I), 0.9),
    ("headroom",     re.compile(r"\b(headroom|capacity review)\b", re.I), 0.85),
    ("second_call",  re.compile(r"\b(2nd call|second call|deep dive|demo #?2)\b", re.I), 0.85),
    ("first_call",   re.compile(r"\b(intro|discovery|first call|initial|1st call|demo #?1)\b", re.I), 0.8),
    ("follow_up",    re.compile(r"\b(follow[- ]?up|check[- ]?in|touchpoint)\b", re.I), 0.8),
    ("sdr_cold_call", re.compile(r"\b(sdr|cold call|prospecting|outbound)\b", re.I), 0.8),
)


# ---------------------------------------------------------------- rule cascade

def _classify_by_rules(t: NormalizedTranscript) -> Classification | None:
    # 1: no external attendees → internal
    if not t.has_external_attendees:
        return Classification("internal", 0.95, "no_external_attendees")

    # 2: title regex cascade (first match wins)
    title = t.title or ""
    for call_type, pattern, conf in _TITLE_PATTERNS:
        if pattern.search(title):
            return Classification(call_type, conf, f"title_match:{pattern.pattern[:40]}")

    # 3: SDR-style short duration heuristic. Fire only if the title gives no signal
    #    and duration < 10 min and there are 1 external attendee.
    if t.duration_minutes is not None and t.duration_minutes < 10 and len(t.external_attendees) <= 1:
        return Classification("sdr_cold_call", 0.55, "short_duration_and_single_external")

    return None


# ---------------------------------------------------------------- Haiku fallback

_HAIKU_SYSTEM = (
    "You classify restaurant-SaaS sales call transcripts for Loop AI. "
    "Given the call title, duration, attendees, and a 2000-character sample, "
    "return JSON with: call_type (one of: first_call, second_call, follow_up, "
    "sdr_cold_call, onboarding, cs, pilot, renewal, internal, headroom, other), "
    "confidence (0..1), reason (one sentence). "
    "Only output JSON, no prose."
)


def _classify_by_haiku(t: NormalizedTranscript) -> Classification:
    from anthropic import Anthropic  # lazy import so tests don't need the key

    api_key = get_config("ANTHROPIC_API_KEY")
    if not api_key:
        # Haiku unavailable (no key, test env) — fall back to 'other' with low confidence.
        return Classification("other", 0.2, "no_anthropic_key_fallback")

    client = Anthropic(api_key=api_key)
    sample = "\n".join(
        f"{s.get('speaker_name','?')}: {s.get('text','')}"
        for s in t.sentences[:50]
    )[:2000]
    user_msg = (
        f"Title: {t.title!r}\n"
        f"Duration: {t.duration_minutes} min\n"
        f"Internal: {t.internal_attendees}\n"
        f"External: {t.external_attendees}\n"
        f"Sample:\n{sample}"
    )
    try:
        resp = client.messages.create(
            model=_HAIKU_MODEL,
            max_tokens=200,
            system=_HAIKU_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if hasattr(b, "text"))
        data = _extract_json(text)
        return Classification(
            call_type=str(data.get("call_type", "other")),
            confidence=float(data.get("confidence", 0.3)),
            reason=f"haiku:{data.get('reason', '')[:120]}",
        )
    except Exception as e:  # noqa: BLE001 — classifier must never break the grader
        log.warning("haiku classifier failed: %s", e)
        return Classification("other", 0.2, f"haiku_error:{type(e).__name__}")


def _extract_json(text: str) -> dict[str, Any]:
    """Strip fences, find first {...} block, parse."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:].strip()
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- public API

def classify(t: NormalizedTranscript, *, allow_haiku: bool = True) -> Classification:
    """Classify a normalized transcript. Rule cascade, Haiku fallback."""
    rule_result = _classify_by_rules(t)
    if rule_result is not None:
        log.debug("classify meeting=%s by_rules=%s", t.meeting_id, rule_result)
        return rule_result
    if allow_haiku:
        result = _classify_by_haiku(t)
        log.debug("classify meeting=%s by_haiku=%s", t.meeting_id, result)
        return result
    return Classification("other", 0.2, "rules_inconclusive_haiku_disabled")
