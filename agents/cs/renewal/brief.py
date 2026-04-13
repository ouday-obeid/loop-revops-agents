"""Renewal brief generator — markdown for CSM pre-call prep.

Pulls account state + last 3 Fireflies calls + open cases + current churn
score into a single markdown doc. Returned as a string so the dispatcher can
post it back to Slack (or write to a tmp file). No side effects.

Consumer: `@oo cs brief <account>` slash path and `renewal/pipeline.brief_sent_at`
timestamp update (tracked separately once M9 wires the Slack upload).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)


def _latest_health(account_id: str) -> dict[str, Any] | None:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT score, nps_score, nps_category, nps_at, last_touch_at, checked_at, name
                     FROM cs_account_health WHERE account_id = :a"""
            ),
            {"a": account_id},
        ).mappings().first()
    return dict(row) if row else None


def _latest_risk(account_id: str) -> dict[str, Any] | None:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT score, tier, factors_json, created_at FROM cs_churn_risk
                    WHERE account_id = :a ORDER BY created_at DESC LIMIT 1"""
            ),
            {"a": account_id},
        ).mappings().first()
    if not row:
        return None
    out = dict(row)
    try:
        out["factors_decoded"] = json.loads(out["factors_json"])
    except (TypeError, ValueError):
        out["factors_decoded"] = {}
    return out


def _open_renewal(sf_mcp: Any, account_id: str) -> dict | None:
    try:
        q = (
            "SELECT Id, Name, StageName, Amount, Zen_Contract_End_Date__c "
            "FROM Opportunity "
            f"WHERE AccountId = '{account_id}' AND Type = 'Renewal' "
            "AND IsClosed = false ORDER BY Zen_Contract_End_Date__c ASC LIMIT 1"
        )
        r = sf_mcp.soql_query(q, limit=1)
        records = r.get("records") or []
        return records[0] if records else None
    except Exception as e:
        log.warning("brief renewal lookup failed: %s", e)
        return None


def _open_cases(sf_mcp: Any, account_id: str) -> list[dict]:
    try:
        q = (
            "SELECT Id, Subject, Priority, CreatedDate FROM Case "
            f"WHERE AccountId = '{account_id}' AND IsClosed = false "
            "ORDER BY CreatedDate DESC LIMIT 5"
        )
        r = sf_mcp.soql_query(q, limit=5)
        return r.get("records") or []
    except Exception as e:
        log.warning("brief cases lookup failed: %s", e)
        return []


def _recent_calls(fireflies_mcp: Any, account_id: str) -> list[dict]:
    try:
        return fireflies_mcp.list_transcripts(account_id=account_id, limit=3) or []
    except Exception as e:
        log.warning("brief fireflies lookup failed: %s", e)
        return []


def _fmt_date(raw: Any) -> str:
    if not raw:
        return "—"
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    return str(raw)[:10]


def generate(
    account_id: str,
    *,
    sf_mcp: Any | None = None,
    fireflies_mcp: Any | None = None,
) -> str:
    """Return a markdown-formatted renewal brief for `account_id`."""
    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf
    if fireflies_mcp is None:
        from shared.mcp import fireflies_mcp as _ff
        fireflies_mcp = _ff

    health = _latest_health(account_id) or {}
    risk = _latest_risk(account_id)
    renewal = _open_renewal(sf_mcp, account_id) or {}
    cases = _open_cases(sf_mcp, account_id)
    calls = _recent_calls(fireflies_mcp, account_id)

    name = health.get("name") or account_id
    lines = [f"# Renewal brief — {name}", ""]
    lines.append(f"_Account:_ `{account_id}` · _Generated:_ {datetime.now(timezone.utc).isoformat(timespec='minutes')}")
    lines.append("")

    lines.append("## Account health")
    lines.append(f"- Vitally score: **{health.get('score', '—')}**")
    lines.append(f"- NPS: **{health.get('nps_score', '—')}** ({health.get('nps_category', 'unknown')})")
    lines.append(f"- Last touch: {_fmt_date(health.get('last_touch_at'))}")
    lines.append("")

    if risk:
        lines.append("## Churn risk")
        lines.append(f"- Score **{risk['score']}** (tier {risk['tier']})")
        contribs = (risk.get("factors_decoded") or {}).get("contributions") or {}
        top = sorted(contribs.items(), key=lambda kv: kv[1], reverse=True)[:3]
        for k, v in top:
            if v > 0:
                lines.append(f"  - `{k}`: {v}")
        lines.append("")

    lines.append("## Renewal")
    if renewal:
        lines.append(f"- Opp: `{renewal.get('Id')}` — {renewal.get('Name')}")
        lines.append(f"- Stage: **{renewal.get('StageName')}**")
        lines.append(f"- Amount: {renewal.get('Amount') or '—'}")
        lines.append(f"- Contract end: {_fmt_date(renewal.get('Zen_Contract_End_Date__c'))}")
    else:
        lines.append("- _No open Renewal opportunity found._")
    lines.append("")

    lines.append("## Recent calls (Fireflies)")
    if calls:
        for c in calls:
            date = _fmt_date(c.get("date") or c.get("dateTime"))
            title = c.get("title") or c.get("meeting_title") or "(untitled)"
            lines.append(f"- {date} — {title}")
    else:
        lines.append("- _No recent calls in Fireflies._")
    lines.append("")

    lines.append("## Open cases")
    if cases:
        for c in cases:
            lines.append(
                f"- `{c.get('Id')}` [{c.get('Priority', '—')}] {c.get('Subject', '(no subject)')}"
            )
    else:
        lines.append("- _No open cases._")
    lines.append("")

    lines.append("## Suggested talking points")
    lines.append("- Confirm renewal term and contract end date")
    lines.append("- Review open cases + resolution progress")
    if risk and risk.get("tier", 0) >= 70:
        lines.append("- **Churn risk elevated** — probe for unmet needs, sponsor stability")
    lines.append("- Introduce expansion levers (locations, brands, seats)")
    return "\n".join(lines)
