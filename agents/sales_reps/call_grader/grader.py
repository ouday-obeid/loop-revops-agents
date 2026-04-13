"""Call grader — Sonnet-backed, rubric-driven, JSON-output.

Flow:
  1. fireflies_adapter.fetch_and_normalize(meeting_id)
  2. classifier.classify(transcript)
  3. If non-gradable → return {'skipped': true, ...}
  4. rubrics.get_rubric(call_type) → Rubric
  5. Call Sonnet with a rubric-shaped prompt; prompt-caching on the rubric block
  6. Parse JSON response; critical-item cap enforced in post
  7. storage.upsert_grade(...) and write audit log entry
  8. Return Slack-renderable summary
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from shared import governance
from shared.secrets import get_config

from agents.sales_reps.call_grader import fireflies_adapter, rubrics, storage
from agents.sales_reps.call_grader.classifier import Classification, classify

log = logging.getLogger(__name__)

_SONNET_MODEL = "claude-sonnet-4-6"

# Pricing: approximate per MTok. Opus would be 3x this.
_SONNET_INPUT_PER_MTOK = 3.0
_SONNET_OUTPUT_PER_MTOK = 15.0

_AGENT_NAME = "sales_reps"

_SYSTEM_PROMPT_TEMPLATE = """You grade sales calls for Loop AI (restaurant-delivery SaaS). \
You score each section on a 1-5 integer scale against its criteria. Be calibrated — the average \
rep scores 2.5-3.0; a 4 is genuinely strong performance; a 5 is exceptional.

Scoring rules:
- Each section: integer score 1..5
- If a critical item in a section is missed, CAP that section at 3 even if other criteria were met
- Provide evidence (direct quotes from the transcript) for every section
- Keep coaching feedback concrete and actionable, not generic
- Keep cell_note to 1-2 sentences, scorecard-style — a manager should be able to drop it into their sheet

Scorecard for this call type: {scorecard_name}

Sections (format: "Name (weight) — criteria; critical items in [brackets]"):
{sections_block}

Output valid JSON matching this shape exactly:
{{
  "call_type_confirmed": "{call_type}",
  "sections": {{
    {sections_example}
  }},
  "overall_strengths": ["...", "..."],
  "overall_improvements": ["...", "..."],
  "critical_misses": ["..."],
  "coaching_summary": "2-3 sentences for the rep"
}}

Return ONLY JSON, no prose.
"""


def _build_system_prompt(rubric: rubrics.Rubric) -> str:
    sections_lines = []
    sections_ex = []
    for s in rubric.sections:
        critical = (
            " [critical: " + " | ".join(s.critical_items) + "]"
            if s.critical_items else ""
        )
        sections_lines.append(
            f'- "{s.name}" (weight {s.weight}) — criteria: {"; ".join(s.criteria)}{critical}'
        )
        sections_ex.append(
            f'    "{s.name}": {{"score": 0, "evidence": ["..."], "feedback": "...", '
            f'"cell_note": "...", "critical_misses": []}}'
        )
    return _SYSTEM_PROMPT_TEMPLATE.format(
        scorecard_name=rubric.scorecard_name,
        sections_block="\n".join(sections_lines),
        sections_example=",\n".join(sections_ex),
        call_type=rubric.call_type,
    )


def _build_user_message(t: fireflies_adapter.NormalizedTranscript) -> str:
    return (
        f"Call metadata\n"
        f"  meeting_id: {t.meeting_id}\n"
        f"  title: {t.title!r}\n"
        f"  date: {t.date}\n"
        f"  duration: {t.duration_minutes} min\n"
        f"  rep: {t.rep_name or t.rep_email}\n"
        f"  internal: {t.internal_attendees}\n"
        f"  external: {t.external_attendees}\n\n"
        f"Transcript\n{t.rendered_text()}"
    )


# ---------------------------------------------------------------- public API

async def grade_one(meeting_id: str, *, allow_haiku: bool = True) -> dict[str, Any]:
    """Grade a single call end-to-end. Returns a Slack-renderable payload."""
    t = fireflies_adapter.fetch_and_normalize(meeting_id)
    cls = classify(t, allow_haiku=allow_haiku)

    if not cls.is_gradable:
        return {
            "text": f"sales_reps: call {meeting_id} is {cls.call_type}, not gradable. "
                    f"(reason: {cls.reason})",
            "skipped": True,
            "call_type": cls.call_type,
            "classifier_reason": cls.reason,
        }

    rubric = rubrics.get_rubric(cls.call_type)
    graded = _invoke_grader(t, rubric)
    summary = _finalize(t, cls, rubric, graded)
    storage.upsert_grade(summary["persistable"])

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_grade_call",
        target=f"fireflies:{meeting_id}",
        after={
            "call_type": cls.call_type,
            "percentage": summary["persistable"]["percentage"],
            "grade_label": summary["persistable"]["pass_fail"],
            "rep_email": summary["persistable"]["rep_email"],
        },
    )
    return summary["slack"]


# ---------------------------------------------------------------- LLM plumbing

def _invoke_grader(t: fireflies_adapter.NormalizedTranscript, rubric: rubrics.Rubric) -> dict[str, Any]:
    from anthropic import Anthropic  # lazy

    api_key = get_config("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot grade")
    client = Anthropic(api_key=api_key)

    system = _build_system_prompt(rubric)
    user_msg = _build_user_message(t)

    start = time.monotonic()
    resp = client.messages.create(
        model=_SONNET_MODEL,
        max_tokens=4000,
        # Cache the system prompt so repeated grades of the same rubric reuse tokens.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    elapsed = time.monotonic() - start
    raw_text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    parsed = _extract_json(raw_text)
    usage = getattr(resp, "usage", None)
    tokens_in = getattr(usage, "input_tokens", 0) if usage else 0
    tokens_out = getattr(usage, "output_tokens", 0) if usage else 0
    cost = (tokens_in / 1e6) * _SONNET_INPUT_PER_MTOK + (tokens_out / 1e6) * _SONNET_OUTPUT_PER_MTOK

    log.info(
        "graded meeting=%s type=%s tokens=%s/%s cost=$%.4f elapsed=%.1fs",
        t.meeting_id, rubric.call_type, tokens_in, tokens_out, cost, elapsed,
    )
    return {
        "llm_json": parsed,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost,
        "model": _SONNET_MODEL,
    }


def _extract_json(text: str) -> dict[str, Any]:
    t = text.strip()
    # Strip markdown fences if the model ignored instruction.
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M)
    start = t.find("{")
    end = t.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        log.warning("grader JSON parse failed: %s — raw[:300]=%s", e, t[:300])
        return {}


# ---------------------------------------------------------------- post-processing

def _finalize(
    t: fireflies_adapter.NormalizedTranscript,
    cls: Classification,
    rubric: rubrics.Rubric,
    graded: dict[str, Any],
) -> dict[str, Any]:
    llm = graded.get("llm_json") or {}
    llm_sections = llm.get("sections") or {}

    section_scores: dict[str, int] = {}
    evidence: dict[str, list[str]] = {}
    feedback: dict[str, str] = {}
    cell_notes: dict[str, str] = {}
    critical_misses: list[str] = []

    for section in rubric.sections:
        s = llm_sections.get(section.name) or {}
        try:
            raw_score = int(s.get("score", 1))
        except (TypeError, ValueError):
            raw_score = 1
        raw_score = max(1, min(5, raw_score))

        misses = [m for m in (s.get("critical_misses") or []) if m]
        if section.critical_items and misses:
            # Enforce the 3-cap if a critical item was missed.
            raw_score = min(raw_score, 3)
        section_scores[section.name] = raw_score
        evidence[section.name] = list(s.get("evidence") or [])
        feedback[section.name] = str(s.get("feedback") or "")
        cell_notes[section.name] = str(s.get("cell_note") or "")
        critical_misses.extend(misses)

    totals = rubrics.compute_weighted_score(rubric, section_scores)

    persistable = {
        "meeting_id": t.meeting_id,
        "rep_email": t.rep_email,
        "rep_name": t.rep_name,
        "call_type": cls.call_type,
        "scorecard_type": rubric.scorecard_name,
        "section_scores": section_scores,
        "weighted_total": totals["weighted_total"],
        "max_weighted": totals["max_weighted"],
        "percentage": totals["percentage"],
        "pass_fail": totals["grade_label"],
        "evidence": evidence,
        "feedback": feedback,
        "strengths": llm.get("overall_strengths") or [],
        "improvements": llm.get("overall_improvements") or [],
        "critical_misses": critical_misses + (llm.get("critical_misses") or []),
        "coaching_summary": str(llm.get("coaching_summary") or ""),
        "cell_notes": cell_notes,
        "model_used": graded.get("model"),
        "tokens_in": graded.get("tokens_in"),
        "tokens_out": graded.get("tokens_out"),
        "cost_usd": graded.get("cost_usd"),
        "transcript_url": t.transcript_url,
        "call_date": t.date,
    }

    # Slack-facing summary
    score_line = (
        f"{totals['percentage']:.0f}% "
        f"({totals['weighted_total']:.1f}/{totals['max_weighted']:.1f}) — "
        f"*{totals['grade_label'].replace('_', ' ')}*"
    )
    sections_sparkline = " ".join(f"{section.name[:3]}={section_scores[section.name]}"
                                  for section in rubric.sections)
    slack = {
        "text": (
            f"*Call graded* — {rubric.scorecard_name}\n"
            f"Rep: {t.rep_name or t.rep_email}  |  {t.title}\n"
            f"Score: {score_line}\n"
            f"Sections: `{sections_sparkline}`\n"
            f"Coaching: {persistable['coaching_summary'][:280]}"
        ),
        "graded": True,
        "meeting_id": t.meeting_id,
        "call_type": cls.call_type,
        "percentage": totals["percentage"],
        "grade_label": totals["grade_label"],
        "section_scores": section_scores,
        "coaching_summary": persistable["coaching_summary"],
    }
    return {"slack": slack, "persistable": persistable}
