"""QBR generator — markdown quarterly business review.

On-demand output for `@oo cs qbr <account>`. Summarizes the last quarter of:
  - Vitally health trajectory (from cs_account_health_history)
  - NPS trend
  - Open + resolved cases
  - Renewal status
  - Call cadence (Fireflies)
  - Top 3 call themes (best-effort: summaries concatenated)

Scope per O (2026-04-13): markdown only, no Slides integration in V1.
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)


def _history(account_id: str, since: datetime) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT score, nps_score, checked_at FROM cs_account_health_history
                    WHERE account_id = :a AND checked_at >= :s
                    ORDER BY checked_at ASC"""
            ),
            {"a": account_id, "s": since},
        ).mappings().all()
    return [dict(r) for r in rows]


def _health_trend(history: list[dict]) -> tuple[str, float | None, float | None]:
    scores = [h["score"] for h in history if h["score"] is not None]
    if len(scores) < 2:
        avg = statistics.mean(scores) if scores else None
        return "insufficient data", avg, None
    start, end = scores[0], scores[-1]
    delta = end - start
    if delta > 3:
        label = "improving"
    elif delta < -3:
        label = "declining"
    else:
        label = "stable"
    return label, statistics.mean(scores), delta


def _case_volume(sf_mcp: Any, account_id: str, since: datetime) -> dict[str, int]:
    try:
        cutoff = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        q_total = (
            f"SELECT COUNT(Id) c FROM Case WHERE AccountId = '{account_id}' "
            f"AND CreatedDate > {cutoff}"
        )
        q_closed = (
            f"SELECT COUNT(Id) c FROM Case WHERE AccountId = '{account_id}' "
            f"AND CreatedDate > {cutoff} AND IsClosed = true"
        )
        total = _count(sf_mcp.soql_query(q_total, limit=1))
        closed = _count(sf_mcp.soql_query(q_closed, limit=1))
        return {"total": total, "closed": closed, "open": max(0, total - closed)}
    except Exception as e:
        log.warning("qbr case volume failed: %s", e)
        return {"total": 0, "closed": 0, "open": 0}


def _count(resp: dict) -> int:
    if not resp:
        return 0
    total = resp.get("totalSize")
    if total:
        return int(total)
    rec = (resp.get("records") or [{}])[0]
    return int(rec.get("c") or 0)


def _call_cadence(fireflies_mcp: Any, account_id: str, since: datetime) -> list[dict]:
    try:
        calls = fireflies_mcp.list_transcripts(
            account_id=account_id, from_date=since.isoformat()
        ) or []
    except Exception as e:
        log.warning("qbr fireflies failed: %s", e)
        return []
    return calls


def _renewal_state(sf_mcp: Any, account_id: str) -> dict | None:
    try:
        q = (
            "SELECT Id, Name, StageName, Amount, Zen_Contract_End_Date__c "
            "FROM Opportunity "
            f"WHERE AccountId = '{account_id}' AND Type = 'Renewal' "
            "ORDER BY CreatedDate DESC LIMIT 1"
        )
        r = sf_mcp.soql_query(q, limit=1)
        recs = r.get("records") or []
        return recs[0] if recs else None
    except Exception as e:
        log.warning("qbr renewal lookup failed: %s", e)
        return None


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
    now: datetime | None = None,
) -> str:
    """Markdown QBR covering the prior 90 days for `account_id`."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=90)

    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf
    if fireflies_mcp is None:
        from shared.mcp import fireflies_mcp as _ff
        fireflies_mcp = _ff

    history = _history(account_id, since)
    trend_label, avg_score, delta = _health_trend(history)
    cases = _case_volume(sf_mcp, account_id, since)
    calls = _call_cadence(fireflies_mcp, account_id, since)
    renewal = _renewal_state(sf_mcp, account_id)

    lines = [
        f"# QBR — account `{account_id}`",
        "",
        f"_Period:_ {since.date().isoformat()} → {now.date().isoformat()} "
        f"· _Generated:_ {now.isoformat(timespec='minutes')}",
        "",
        "## Vitally health",
        f"- Trajectory: **{trend_label}**",
        f"- 90d average: {avg_score:.1f}" if avg_score is not None else "- 90d average: —",
    ]
    if delta is not None:
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(f"- Net delta: {arrow} {delta:+.1f}")
    lines.append("")

    lines.append("## Case volume")
    lines.append(f"- Created: **{cases['total']}**")
    lines.append(f"- Closed: {cases['closed']}")
    lines.append(f"- Still open: {cases['open']}")
    lines.append("")

    lines.append("## Call cadence (Fireflies)")
    lines.append(f"- Calls in period: **{len(calls)}**")
    for c in calls[:5]:
        date = _fmt_date(c.get("date") or c.get("dateTime"))
        title = c.get("title") or c.get("meeting_title") or "(untitled)"
        lines.append(f"  - {date} — {title}")
    lines.append("")

    lines.append("## Renewal")
    if renewal:
        lines.append(f"- Opp `{renewal.get('Id')}` — {renewal.get('Name')}")
        lines.append(f"- Stage: **{renewal.get('StageName')}**")
        lines.append(f"- Contract end: {_fmt_date(renewal.get('Zen_Contract_End_Date__c'))}")
        lines.append(f"- Amount: {renewal.get('Amount') or '—'}")
    else:
        lines.append("- _No Renewal opportunity on record._")
    lines.append("")

    lines.append("## Recommended next actions")
    if trend_label == "declining":
        lines.append("- Health declining — schedule exec-level check-in this week")
    if cases["open"] > 5:
        lines.append(f"- {cases['open']} open cases — coordinate with support on resolution plan")
    if len(calls) < 3:
        lines.append("- Call cadence low — propose bi-weekly standing sync")
    if not lines[-1].startswith("- "):
        lines.append("- _None flagged automatically; review manually before QBR._")

    return "\n".join(lines)
