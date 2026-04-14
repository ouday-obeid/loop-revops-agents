"""Call Intel pillar — maps Fireflies transcripts → PillarScore per opp.

Flow:
  1. For each OppRecord, collect transcripts where a participant email matches
     an OpportunityContactRoles.Contact.Email (case-insensitive).
  2. Score the bundle on keyword hits (positive + negative), champion presence
     (≥ CALL_CHAMPION_MIN_TRANSCRIPTS distinct buyer emails inside the
     CALL_CHAMPION_WINDOW), and rep-owned action items.
  3. If the composite sits in CALL_CLASSIFIER_AMBIGUOUS_RANGE AND the opp is
     in the top CALL_CLASSIFIER_TOP_N_BY_ACV by ACV, call the optional Haiku
     classifier (caller supplies a callable — we don't bake in a model SDK).

Returns a `CallIntelSignal` per opp + a keyed map suitable for
`scorer.score_all(call_overrides=…)`.

Kept deliberately pure: every I/O call is behind a caller-supplied function
(`list_transcripts_fn`, `classifier_fn`) so tests run without Fireflies auth
and backtest replay hits the same path.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from agents.slt_metrics.pipeline.config import (
    CALL_ACTION_ITEMS_BONUS,
    CALL_CHAMPION_BONUS,
    CALL_CHAMPION_MIN_TRANSCRIPTS,
    CALL_CHAMPION_WINDOW_DAYS,
    CALL_CLASSIFIER_AMBIGUOUS_RANGE,
    CALL_CLASSIFIER_TOP_N_BY_ACV,
    CALL_LOOKBACK_DAYS,
    CALL_NEGATIVE_KEYWORDS,
    CALL_NEGATIVE_PENALTY,
    CALL_POSITIVE_KEYWORDS,
    CALL_POSITIVE_MAX_BONUS,
)
from agents.slt_metrics.types import CallIntelSignal, OppRecord, PillarScore

log = logging.getLogger(__name__)

# Callable signatures exposed to callers so the Fireflies MCP stays behind an
# injection boundary. list_transcripts_fn returns dicts shaped like the
# `fireflies_mcp.get_transcript` response (id, date, participants, summary).
ListTranscriptsFn = Callable[[str, str, str], list[dict[str, Any]]]
# Args: participant_email, from_iso, to_iso
ClassifierFn = Callable[[OppRecord, list[dict[str, Any]]], dict[str, Any]]


@dataclass
class _Score:
    value: float
    keyword_hits: list[str]
    champion_present: bool
    rep_action_items: int
    negative_hits: list[str]
    classifier_verdict: dict[str, Any] | None


def score_call_intel(
    opps: Iterable[OppRecord],
    *,
    today: date,
    list_transcripts_fn: ListTranscriptsFn,
    classifier_fn: ClassifierFn | None = None,
    lookback_days: int = CALL_LOOKBACK_DAYS,
) -> tuple[dict[str, PillarScore], list[CallIntelSignal]]:
    """Return (pillar_overrides, signals).

    pillar_overrides is keyed by opp.id for direct passthrough to
    `scorer.score_all(call_overrides=…)`.
    """
    opps_list = list(opps)
    classifier_cohort = _top_n_by_acv(opps_list, CALL_CLASSIFIER_TOP_N_BY_ACV)
    period_from = (today - timedelta(days=lookback_days)).isoformat()
    period_to = today.isoformat()

    overrides: dict[str, PillarScore] = {}
    signals: list[CallIntelSignal] = []

    for opp in opps_list:
        transcripts = _collect_transcripts(
            opp, period_from=period_from, period_to=period_to,
            list_transcripts_fn=list_transcripts_fn,
        )
        scored = _score_one(opp, transcripts, today=today)

        if (
            classifier_fn is not None
            and opp.id in classifier_cohort
            and _in_ambiguous_range(scored.value)
        ):
            try:
                verdict = classifier_fn(opp, transcripts)
            except Exception:  # noqa: BLE001 — classifier errors should not crash the pipeline
                log.exception("call_intel classifier failed for opp=%s", opp.id)
                verdict = None
            if verdict is not None:
                scored = _apply_classifier_verdict(scored, verdict)

        detail = _format_detail(scored, n=len(transcripts))
        overrides[opp.id] = PillarScore(value=scored.value, detail=detail)
        signals.append(
            CallIntelSignal(
                opp_id=opp.id,
                transcripts_considered=len(transcripts),
                keyword_hits=scored.keyword_hits,
                champion_present=scored.champion_present,
                rep_action_items=scored.rep_action_items,
                negative_hits=scored.negative_hits,
                classifier_verdict=scored.classifier_verdict,
                score_delta=scored.value,
            )
        )
    return overrides, signals


# ------------------------------------------------------------------ collection

def _collect_transcripts(
    opp: OppRecord,
    *,
    period_from: str,
    period_to: str,
    list_transcripts_fn: ListTranscriptsFn,
) -> list[dict[str, Any]]:
    """Union of transcripts across every contact-role email."""
    emails = {
        (cr.email or "").strip().lower()
        for cr in opp.contact_roles
        if cr.email
    }
    emails.discard("")
    if not emails:
        return []

    seen: dict[str, dict[str, Any]] = {}
    for email in emails:
        try:
            rows = list_transcripts_fn(email, period_from, period_to) or []
        except Exception:  # noqa: BLE001 — downstream shouldn't blow up on Fireflies hiccups
            log.warning("call_intel transcripts fetch failed for %s", email)
            rows = []
        for t in rows:
            tid = str(t.get("id") or "")
            if tid and tid not in seen:
                seen[tid] = t
    return list(seen.values())


# ------------------------------------------------------------------ scoring

def _score_one(
    opp: OppRecord,
    transcripts: list[dict[str, Any]],
    *,
    today: date,
) -> _Score:
    if not transcripts:
        return _Score(
            value=0.0,
            keyword_hits=[],
            champion_present=False,
            rep_action_items=0,
            negative_hits=[],
            classifier_verdict=None,
        )

    # Keyword hits on `summary.keywords` (+short_summary for resilience) —
    # case-insensitive substring match.
    keyword_hits = _positive_keyword_hits(transcripts)
    negative_hits = _negative_keyword_hits(transcripts)
    champion_present = _champion_present(opp, transcripts, today=today)
    rep_action_items = _count_rep_action_items(opp, transcripts)

    # Positive bonus scales with distinct hits, capped at CALL_POSITIVE_MAX_BONUS.
    positive_hit_count = len(keyword_hits)
    positive_bonus = min(
        CALL_POSITIVE_MAX_BONUS,
        positive_hit_count * (CALL_POSITIVE_MAX_BONUS / max(1, len(CALL_POSITIVE_KEYWORDS))),
    )
    champion_bonus = CALL_CHAMPION_BONUS if champion_present else 0.0
    action_bonus = CALL_ACTION_ITEMS_BONUS if rep_action_items > 0 else 0.0
    negative_penalty = CALL_NEGATIVE_PENALTY if negative_hits else 0.0

    raw = positive_bonus + champion_bonus + action_bonus - negative_penalty
    value = max(0.0, min(1.0, raw))

    return _Score(
        value=value,
        keyword_hits=keyword_hits,
        champion_present=champion_present,
        rep_action_items=rep_action_items,
        negative_hits=negative_hits,
        classifier_verdict=None,
    )


def _positive_keyword_hits(transcripts: list[dict[str, Any]]) -> list[str]:
    hits: set[str] = set()
    for t in transcripts:
        text = _summary_text(t).lower()
        for kw in CALL_POSITIVE_KEYWORDS:
            if kw in text:
                hits.add(kw)
    return sorted(hits)


def _negative_keyword_hits(transcripts: list[dict[str, Any]]) -> list[str]:
    hits: set[str] = set()
    for t in transcripts:
        text = _summary_text(t).lower()
        for kw in CALL_NEGATIVE_KEYWORDS:
            if kw in text:
                hits.add(kw)
    return sorted(hits)


def _summary_text(t: dict[str, Any]) -> str:
    s = t.get("summary") or {}
    parts = [
        _join(s.get("keywords")),
        s.get("overview") or "",
        s.get("short_summary") or "",
        s.get("bullet_gist") or "",
    ]
    return " ".join(p for p in parts if p)


def _join(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    if value is None:
        return ""
    return str(value)


def _champion_present(
    opp: OppRecord,
    transcripts: list[dict[str, Any]],
    *,
    today: date,
) -> bool:
    """≥ CALL_CHAMPION_MIN_TRANSCRIPTS distinct buyer-side emails within window.

    Rep-side emails (any @tryloop.ai / @loop.ai) are excluded so an internal
    team member on every call doesn't count as a "champion". Unused param `opp`
    kept for a future rep-identity heuristic (e.g., owner email → skip explicitly).
    """
    del opp  # reserved for owner-email-based filtering once we thread it through
    window_start = today - timedelta(days=CALL_CHAMPION_WINDOW_DAYS)
    buyer_emails: set[str] = set()
    for t in transcripts:
        tdate = _parse_transcript_date(t.get("date"))
        if tdate is None or tdate < window_start:
            continue
        for email in _participant_emails(t):
            if _is_rep_email(email):
                continue
            buyer_emails.add(email)
    return len(buyer_emails) >= CALL_CHAMPION_MIN_TRANSCRIPTS


def _is_rep_email(email: str) -> bool:
    if not email:
        return False
    _, _, domain = email.rpartition("@")
    return domain.lower() in _LOOP_DOMAINS


def _participant_emails(t: dict[str, Any]) -> list[str]:
    parts = t.get("participants") or []
    emails: list[str] = []
    for p in parts:
        if isinstance(p, str):
            emails.append(p.lower().strip())
        elif isinstance(p, dict):
            e = p.get("email") or p.get("Email") or ""
            if e:
                emails.append(str(e).lower().strip())
    return [e for e in emails if e and "@" in e]


_LOOP_DOMAINS = frozenset({"tryloop.ai", "loop.ai"})


def _count_rep_action_items(opp: OppRecord, transcripts: list[dict[str, Any]]) -> int:
    """Action items where the assignee email matches a Loop internal domain."""
    total = 0
    for t in transcripts:
        items = (t.get("summary") or {}).get("action_items") or []
        if isinstance(items, str):
            # Older Fireflies responses stringify the list — split on common separators.
            items = [s.strip() for s in items.replace("\n", ";").split(";") if s.strip()]
        for item in items:
            text = item if isinstance(item, str) else (item.get("text") if isinstance(item, dict) else "")
            if not text:
                continue
            if any(dom in text.lower() for dom in _LOOP_DOMAINS):
                total += 1
            elif ":" in text[:40] and "@" in text[:40]:
                # Pattern "jane@tryloop.ai: do X" — count only if our domain appears.
                if any(dom in text[:40].lower() for dom in _LOOP_DOMAINS):
                    total += 1
    return total


def _parse_transcript_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value)
    # Fireflies returns ISO 8601 strings (`2026-04-09T15:00:00Z`).
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ------------------------------------------------------------------ classifier gating

def _top_n_by_acv(opps: list[OppRecord], n: int) -> frozenset[str]:
    sorted_opps = sorted(opps, key=lambda o: (o.acv or 0.0), reverse=True)
    return frozenset(o.id for o in sorted_opps[:n])


def _in_ambiguous_range(value: float) -> bool:
    lo, hi = CALL_CLASSIFIER_AMBIGUOUS_RANGE
    return lo <= value <= hi


def _apply_classifier_verdict(scored: _Score, verdict: Mapping[str, Any]) -> _Score:
    """Fold Haiku output into the pillar score.

    Haiku returns {champion_strength, mutual_plan, decision_authority} each 0–1.
    Average the three and blend 50/50 with the keyword-driven score — the
    classifier is a tiebreaker, not a replacement.
    """
    fields = ("champion_strength", "mutual_plan", "decision_authority")
    vals = [float(verdict.get(f, 0.0) or 0.0) for f in fields]
    vals = [max(0.0, min(1.0, v)) for v in vals]
    classifier_score = sum(vals) / len(fields)
    blended = 0.5 * scored.value + 0.5 * classifier_score
    return _Score(
        value=max(0.0, min(1.0, blended)),
        keyword_hits=scored.keyword_hits,
        champion_present=scored.champion_present,
        rep_action_items=scored.rep_action_items,
        negative_hits=scored.negative_hits,
        classifier_verdict=dict(verdict),
    )


# ------------------------------------------------------------------ detail string

def _format_detail(scored: _Score, *, n: int) -> str:
    if n == 0:
        return "no-transcripts"
    parts = [f"{n}tx"]
    if scored.keyword_hits:
        parts.append("+".join(scored.keyword_hits))
    if scored.champion_present:
        parts.append("champ")
    if scored.rep_action_items:
        parts.append(f"ai={scored.rep_action_items}")
    if scored.negative_hits:
        parts.append("-" + ",".join(scored.negative_hits))
    if scored.classifier_verdict is not None:
        parts.append("haiku")
    return " ".join(parts)
