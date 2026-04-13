"""15-category pain classifier for Board Monitor.

Phase 0 ships pattern-match baseline. LLM fallback is a stub — wire to
Anthropic SDK when token budget is confirmed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CATEGORIES = [
    "data_quality",
    "sf_reports_missing",
    "automation_broken",
    "urgent_fire",
    "access_request",
    "enrichment_request",
    "sop_missing",
    "onboarding_missing",
    "account_hierarchy_issue",
    "integration_broken",
    "call_issue",
    "pipeline_hygiene",
    "commission_issue",
    "renewal_issue",
    "other",
]

ALERT_CATEGORIES = {"urgent_fire", "automation_broken", "integration_broken"}


@dataclass
class Classification:
    category: str
    confidence: float
    matched_phrase: str | None = None


_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("urgent_fire", re.compile(r"\b(urgent|asap|on fire|blocker|blocking|broken now|p0|sev1|sev-1)\b", re.I)),
    ("automation_broken", re.compile(r"\b(flow (is )?broken|workflow (is )?broken|automation (not )?(working|firing)|trigger (not )?fir(ing|ed))\b", re.I)),
    ("integration_broken", re.compile(r"\b(salesforce|vitally|fireflies|momentum|nooks|clay|apollo)\b.*\b(down|not syncing|auth (fail|error)|disconnect|sync (broken|failed))\b", re.I)),
    ("data_quality", re.compile(r"\b(dup(e|licate)|missing field|bad data|wrong (owner|stage|amount)|100% hidden)\b", re.I)),
    ("sf_reports_missing", re.compile(r"\b(can('?| no)t find .*report|need .*report|report .*missing|dashboard (broken|wrong))\b", re.I)),
    ("access_request", re.compile(r"\b(give (me )?access|permission|can('?| no)t (see|access)|403|forbidden)\b", re.I)),
    ("enrichment_request", re.compile(r"\b(enrich|lookup|find (phone|email|linkedin)|clay|apollo|zoominfo)\b", re.I)),
    ("sop_missing", re.compile(r"\b(what'?s the process|how do (i|we)|sop|playbook|documented|no documentation)\b", re.I)),
    ("onboarding_missing", re.compile(r"\b(onboard(ing)?|new hire|ramp|first week)\b", re.I)),
    ("account_hierarchy_issue", re.compile(r"\b(parent account|hierarchy|ultimate parent|account tree|sub[- ]?account)\b", re.I)),
    ("call_issue", re.compile(r"\b(call (recording|transcript)|fireflies|momentum)\b.*\b(missing|not|can('?| no)t)\b", re.I)),
    ("pipeline_hygiene", re.compile(r"\b(stale (opp|deal)|no (next step|activity)|close date|past due|overdue opp)\b", re.I)),
    ("commission_issue", re.compile(r"\b(commission|quota|comp plan|payout|spiff)\b", re.I)),
    ("renewal_issue", re.compile(r"\b(renewal|churn|at risk|expir(ing|ed)|auto-?renew)\b", re.I)),
]


def classify(text: str) -> Classification:
    t = text or ""
    for cat, pat in _PATTERNS:
        m = pat.search(t)
        if m:
            return Classification(category=cat, confidence=0.85, matched_phrase=m.group(0))
    return Classification(category="other", confidence=0.2)


def is_alertworthy(c: Classification) -> bool:
    return c.category in ALERT_CATEGORIES or (
        c.category == "data_quality" and c.matched_phrase and "100%" in c.matched_phrase
    )
