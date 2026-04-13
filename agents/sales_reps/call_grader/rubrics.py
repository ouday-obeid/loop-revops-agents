"""Call-grading rubrics — reused from Outbounder's calibrated scorecards.

Four gradable call types, each with its own section weights, critical items,
and score-to-grade thresholds. Data-only module; no side effects. The grader
imports `get_rubric(call_type)` and feeds the resulting `Rubric` into the LLM
prompt template.

Per plan: call-type taxonomy confirmed as Outbounder's 4-type set
(first_call / second_call / follow_up / sdr_cold_call). Non-gradable types
(onboarding / cs / pilot / renewal / internal / headroom / other) are filtered
upstream in the classifier and never reach the grader.

Scoring:
  - 1-5 integer per section
  - weighted_total = sum(section.score * section.weight)
  - max_weighted = sum(section.max_score * section.weight) = sum(5 * weight)
  - percentage = weighted_total / max_weighted * 100
  - thresholds: ≥70 pass_excellent | ≥50 pass_good | ≥35 fail_needs_work | <35 fail_major_gaps
  - critical-item cap: if any critical item in a section is missed, section.score caps at 3
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CallType = Literal["first_call", "second_call", "follow_up", "sdr_cold_call"]
NonGradable = Literal["onboarding", "cs", "pilot", "renewal", "internal", "headroom", "other"]
AnyCallType = Literal[
    "first_call", "second_call", "follow_up", "sdr_cold_call",
    "onboarding", "cs", "pilot", "renewal", "internal", "headroom", "other",
]


@dataclass(frozen=True)
class Section:
    name: str
    weight: float
    criteria: tuple[str, ...]
    critical_items: tuple[str, ...] = ()
    max_score: int = 5


@dataclass(frozen=True)
class Rubric:
    call_type: CallType
    scorecard_name: str
    sections: tuple[Section, ...]
    pass_thresholds: tuple[tuple[float, str], ...] = (
        (70.0, "pass_excellent"),
        (50.0, "pass_good"),
        (35.0, "fail_needs_work"),
        (0.0, "fail_major_gaps"),
    )

    @property
    def max_weighted(self) -> float:
        return sum(s.max_score * s.weight for s in self.sections)

    def grade_label(self, percentage: float) -> str:
        for threshold, label in self.pass_thresholds:
            if percentage >= threshold:
                return label
        return "fail_major_gaps"

    def section_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.sections)


# ---------------------------------------------------------------- First Call (AE)

FIRST_CALL = Rubric(
    call_type="first_call",
    scorecard_name="AE Certification — First Call",
    sections=(
        Section(
            name="Introduction & Upfront Agenda",
            weight=0.5,
            criteria=(
                "Warm personal intro, builds rapport in first 90 seconds",
                "Sets upfront agenda and asks for what the prospect wants to cover",
                "Confirms time available and decision-maker context",
            ),
            critical_items=(
                "Asks prospect what they want to discuss / their agenda",
            ),
        ),
        Section(
            name="Discovery",
            weight=1.5,
            criteria=(
                "Uncovers # of locations and brand portfolio",
                "Captures AUV / revenue signal per location",
                "Probes 3rd-party delivery mix (DoorDash, UberEats, Grubhub) and % of revenue",
                "Uncovers current tech stack (POS, accounting, analytics)",
                "Quantifies reconciliation pain — hours/week, who owns it",
                "Uncovers marketing spend structure (DSP, promos, LTO budget)",
                "Identifies decision-makers and buying process",
                "Captures chargeback volume or dispute exposure",
                "Uncovers growth plan — openings, M&A, expansion",
                "Probes pain around margin visibility vs. volume mindset",
            ),
            critical_items=(
                "Captures # of locations and delivery mix",
                "Identifies a named, quantified pain point",
            ),
        ),
        Section(
            name="Pitch / Slides",
            weight=1.0,
            criteria=(
                "Origin story — why Loop exists, anchored in chargebacks/reconciliation",
                "Pain resonance — mirrors the prospect's discovery in the pitch",
                "Covers all three pillars: MEASURE (Balance), PROTECT (Recover & Guard), GROW (TruROI, PlanGen)",
                "Connects each pillar to pain uncovered, not feature tour",
            ),
        ),
        Section(
            name="Demo / Platform Walkthrough",
            weight=1.5,
            criteria=(
                "Role-based: pitched to the right persona (CFO / CMO / COO) for the room",
                "Balance sheet walkthrough — journal entries, reconciliation output",
                "TruROI ranking across channels with specific revenue impact",
                "PlanGen agentic automation shown in context",
                "Handles live platform glitches gracefully if any",
            ),
            critical_items=(
                "Demo is tailored to the role in the room, not a generic tour",
            ),
        ),
        Section(
            name="Objection Handling",
            weight=1.0,
            criteria=(
                "Acknowledges objection without defensiveness",
                "Reframes using evidence from discovery",
                "Confirms resolution before moving on",
                "Handles the big three: 'we use X already' / 'send me info' / 'bad timing'",
            ),
        ),
        Section(
            name="Close & Deal Progression",
            weight=0.5,
            criteria=(
                "Sets a specific next step with calendar hold before ending",
                "Confirms attendees and decision process for next meeting",
                "Clarifies timeline to decision",
            ),
            critical_items=(
                "Locks a specific date/time for next step, not vague 'circle back'",
            ),
        ),
    ),
)

# --------------------------------------------------------------- Second Call (AE)

SECOND_CALL = Rubric(
    call_type="second_call",
    scorecard_name="AE — Second Call Deep Dive",
    sections=(
        Section(
            name="Re-Engagement & Agenda Setting",
            weight=0.5,
            criteria=(
                "Re-confirms attendee list and roles on the call",
                "Recaps prior discovery and asks what has changed",
                "Sets explicit agenda for this session",
            ),
        ),
        Section(
            name="Deepened Discovery",
            weight=1.5,
            criteria=(
                "Captures goals and metrics the prospect is judged on",
                "Probes marketing spend structure in detail (DSP, promos, cannibalization concerns)",
                "Quantifies reconciliation pain with current hours and FTE cost",
                "Identifies all decision-makers and the buying criteria",
                "Uncovers margin vs. volume mindset",
                "Validates budget ownership and fiscal timing",
            ),
            critical_items=(
                "Captures margin vs. volume framing — critical for TruROI pitch",
                "Confirms budget owner for Phase 2",
            ),
        ),
        Section(
            name="Focused Demo — Marketing & Analytics",
            weight=1.5,
            criteria=(
                "TruROI / attribution walkthrough with prospect's channels",
                "Marketplace ranking and retention metrics discussion",
                "Cannibalization risk analysis",
                "PlanGen agentic automation demo in context",
                "DSP income statement — prospect can see their own potential output",
            ),
            critical_items=(
                "Ties analytics output to a decision they'd make this quarter",
            ),
        ),
        Section(
            name="Objection Handling",
            weight=1.0,
            criteria=(
                "Handles pricing objections with value anchoring",
                "Handles 'we'll build internally' with TCO reframe",
                "Handles 'we already use X' with differentiation specifics",
                "Confirms resolution before advancing",
            ),
        ),
        Section(
            name="Scope Conversation & Pricing",
            weight=1.0,
            criteria=(
                "Establishes scope before price — # locations, modules, integrations",
                "Price anchored to value, not cost",
                "Addresses budget window and procurement process",
            ),
            critical_items=(
                "Price is only introduced after scope is clear",
            ),
        ),
        Section(
            name="Close & Deal Progression",
            weight=0.5,
            criteria=(
                "Commits next step (contract review, MSA, procurement intro)",
                "Confirms timeline to decision",
                "Sets calendar hold before ending",
            ),
        ),
    ),
)

# ------------------------------------------------------------------ Follow-Up 2.0

FOLLOW_UP = Rubric(
    call_type="follow_up",
    scorecard_name="Follow-Up Rubric 2.0",
    sections=(
        Section(
            name="Opener",
            weight=1.0,
            criteria=(
                "Warm, rapport-building reconnection",
                "References prior conversation with specifics",
                "Confirms time available",
            ),
        ),
        Section(
            name="Discovery WHY (Anchor Motivation)",
            weight=1.0,
            criteria=(
                "Uncovers emotional and practical drivers for change",
                "Connects prior pain to current urgency",
                "Probes what has changed since last call",
            ),
            critical_items=(
                "Surfaces at least one net-new signal since prior call",
            ),
        ),
        Section(
            name="Pain Points Uncovered",
            weight=1.0,
            criteria=(
                "Personalized pain points, not generic",
                "Prospect confirms the pain in their own words",
                "Pain is quantified (hours, dollars, FTE)",
            ),
        ),
        Section(
            name="Confirm Understanding",
            weight=1.0,
            criteria=(
                "Empathetic recap of pain and context",
                "Prospect agrees to the framing",
                "AE demonstrates active listening",
            ),
        ),
        Section(
            name="Pitch (connects to pain, before/after narrative)",
            weight=1.0,
            criteria=(
                "Pitch explicitly references the pain uncovered",
                "Before/after contrast specific to prospect's operation",
                "Avoids feature-listing without pain connection",
            ),
        ),
        Section(
            name="Establish Clear Value Props",
            weight=1.0,
            criteria=(
                "Value props are outcome-focused (revenue recovered, hours saved)",
                "Tied to metrics the prospect cares about",
                "Differentiates from alternatives",
            ),
        ),
        Section(
            name="Transition to Close",
            weight=1.0,
            criteria=(
                "Guided momentum toward commitment",
                "Confident, not pushy",
                "Trial-close language earlier in the conversation",
            ),
        ),
        Section(
            name="Did Next Call Hold?",
            weight=1.0,
            criteria=(
                "Next call scheduled with specific date and time",
                "Prospect shows up (graded post-facto)",
                "Attendees confirmed in advance",
            ),
            critical_items=(
                "Scheduled on calendar before this call ended, not 'we'll circle back'",
            ),
        ),
    ),
)

# ---------------------------------------------------------------- SDR Cold Call

SDR_COLD_CALL = Rubric(
    call_type="sdr_cold_call",
    scorecard_name="SDR Cold Call",
    sections=(
        Section(
            name="Opening & Hook",
            weight=1.0,
            criteria=(
                "Personalized opener with named prospect context",
                "Pattern interrupt — stands out from typical SDR openers",
                "Engagement question within first 10 seconds",
            ),
            critical_items=(
                "Hook is tailored, not scripted generic opener",
            ),
        ),
        Section(
            name="Value Proposition & Relevance",
            weight=1.0,
            criteria=(
                "Concise pitch under 30 seconds",
                "Tailored to prospect's restaurant type or volume",
                "Clear pain connection, not feature list",
            ),
        ),
        Section(
            name="Qualification & Discovery",
            weight=1.0,
            criteria=(
                "Captures # locations",
                "Probes 3rd-party delivery volume",
                "Uncovers a pain point (chargebacks, reconciliation, attribution)",
                "Confirms decision-maker authority or path to decision-maker",
            ),
            critical_items=(
                "Confirms decision-maker authority or identifies who to route to",
            ),
        ),
        Section(
            name="Objection Handling",
            weight=1.0,
            criteria=(
                "Handles 'not interested' with one reframe attempt",
                "Handles 'send me info' by pivoting to discovery",
                "Handles 'we already have a solution' with a differentiating question",
            ),
        ),
        Section(
            name="Close & Meeting Set",
            weight=1.0,
            criteria=(
                "Specific date and time proposed, not 'next week'",
                "Calendar invite sent or confirmed before hanging up",
                "Confirms who else should attend",
            ),
            critical_items=(
                "Meeting locked with calendar invite before call end",
            ),
        ),
    ),
)


# ---------------------------------------------------------------- lookup + helpers

_RUBRICS: dict[str, Rubric] = {
    "first_call": FIRST_CALL,
    "second_call": SECOND_CALL,
    "follow_up": FOLLOW_UP,
    "sdr_cold_call": SDR_COLD_CALL,
}

GRADABLE_TYPES: frozenset[str] = frozenset(_RUBRICS.keys())
NON_GRADABLE_TYPES: frozenset[str] = frozenset(
    {"onboarding", "cs", "pilot", "renewal", "internal", "headroom", "other"}
)


def get_rubric(call_type: str) -> Rubric:
    """Return the rubric for a gradable call type. Raises for non-gradables."""
    if call_type not in _RUBRICS:
        raise ValueError(
            f"No rubric for call_type={call_type!r}. Gradable: {sorted(GRADABLE_TYPES)}"
        )
    return _RUBRICS[call_type]


def is_gradable(call_type: str) -> bool:
    return call_type in GRADABLE_TYPES


def compute_weighted_score(rubric: Rubric, section_scores: dict[str, int]) -> dict:
    """Score a graded call. Returns weighted_total, max_weighted, percentage, grade_label.

    Missing sections are treated as 1 (worst) to avoid rewarding incomplete grading —
    this matches Outbounder's calibration.
    """
    weighted = 0.0
    for section in rubric.sections:
        raw = int(section_scores.get(section.name, 1))
        raw = max(1, min(5, raw))  # clamp defensively
        weighted += raw * section.weight
    max_w = rubric.max_weighted
    pct = (weighted / max_w) * 100 if max_w else 0.0
    return {
        "weighted_total": round(weighted, 2),
        "max_weighted": round(max_w, 2),
        "percentage": round(pct, 1),
        "grade_label": rubric.grade_label(pct),
    }
