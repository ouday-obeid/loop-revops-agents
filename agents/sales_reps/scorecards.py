"""Per-rep Friday scorecards — DM'd to each rep 17:00 ET.

Scope: weekly grade summary + 4-week trend + best/worst call + aggregated
coaching themes. Read-only; no SF writes. Output dict is serializable and
carries `text` for direct Slack posting.

Hutch wants scorecards gated behind his review for the first 4 weeks of
rollout (see RUNBOOK) — that gate lives in the scheduler, not here. This
module just computes and renders.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared import governance
from shared.db.connection import get_engine

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"

_WEEK_WINDOW_DAYS = 7
_TREND_WINDOW_DAYS = 28  # trailing 4 weeks for the comparison baseline


@dataclass
class CallSummary:
    meeting_id: str
    call_type: str
    percentage: float | None
    pass_fail: str | None
    call_date: str | None
    coaching_summary: str | None
    critical_misses: list[str]


def _parse_list_field(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc)
    return end - timedelta(days=days), end


def _query_grades(rep_email: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT meeting_id, call_type, percentage, pass_fail, call_date,
                          coaching_summary, critical_misses
                   FROM sales_reps_call_grades
                   WHERE rep_email = :r AND graded_at >= :s AND graded_at < :e
                   ORDER BY graded_at DESC"""
            ),
            {"r": rep_email, "s": start, "e": end},
        ).mappings().all()
    return [dict(r) for r in rows]


def _as_summary(row: dict[str, Any]) -> CallSummary:
    return CallSummary(
        meeting_id=row["meeting_id"],
        call_type=row.get("call_type") or "unknown",
        percentage=float(row["percentage"]) if row.get("percentage") is not None else None,
        pass_fail=row.get("pass_fail"),
        call_date=row.get("call_date").isoformat() if hasattr(row.get("call_date"), "isoformat")
                  else row.get("call_date"),
        coaching_summary=row.get("coaching_summary"),
        critical_misses=_parse_list_field(row.get("critical_misses")),
    )


def _avg(rows: list[CallSummary]) -> float | None:
    vals = [r.percentage for r in rows if r.percentage is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _best_worst(rows: list[CallSummary]) -> tuple[CallSummary | None, CallSummary | None]:
    graded = [r for r in rows if r.percentage is not None]
    if not graded:
        return None, None
    graded.sort(key=lambda r: r.percentage, reverse=True)
    return graded[0], graded[-1]


def _coaching_themes(rows: list[CallSummary], *, limit: int = 3) -> list[str]:
    """Pull up to `limit` distinct coaching_summary snippets — signal, not word-cloud."""
    seen: set[str] = set()
    out: list[str] = []
    for r in rows:
        cs = (r.coaching_summary or "").strip()
        if not cs:
            continue
        key = cs[:80].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cs[:240])
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------- rendering

def _render(
    rep_email: str,
    week_rows: list[CallSummary],
    week_avg: float | None,
    trend_avg: float | None,
    best: CallSummary | None,
    worst: CallSummary | None,
    themes: list[str],
) -> str:
    if not week_rows:
        return (
            f"*Your weekly scorecard — {rep_email}*\n"
            f"  _No graded calls this week. If you made calls, check that Fireflies "
            f"captured them._"
        )

    lines = [f"*Your weekly scorecard — {rep_email}*"]
    line2 = f"  · {len(week_rows)} calls graded"
    if week_avg is not None:
        line2 += f"  · avg {week_avg:.0f}%"
    if trend_avg is not None and week_avg is not None:
        delta = week_avg - trend_avg
        arrow = "▲" if delta > 2 else ("▼" if delta < -2 else "→")
        line2 += f"  · 4w trend: {trend_avg:.0f}% {arrow}"
    lines.append(line2)

    by_type: dict[str, list[CallSummary]] = {}
    for r in week_rows:
        by_type.setdefault(r.call_type, []).append(r)
    lines.append("\n*This week by call type*")
    for ct, rows in by_type.items():
        ct_avg = _avg(rows)
        avg_txt = f"{ct_avg:.0f}%" if ct_avg is not None else "—"
        lines.append(f"   - {ct}: {len(rows)} calls · {avg_txt}")

    if best and worst and best is not worst:
        lines.append("\n*Highlights*")
        lines.append(f"   - Best: `{best.meeting_id}` · {best.call_type} · {best.percentage:.0f}%")
        lines.append(f"   - Watch: `{worst.meeting_id}` · {worst.call_type} · {worst.percentage:.0f}%")

    crit = [r for r in week_rows if r.critical_misses]
    if crit:
        lines.append(f"\n*Critical misses — {len(crit)} call(s)*")
        for r in crit[:3]:
            lines.append(f"   - `{r.meeting_id}` · {', '.join(r.critical_misses[:2])}")

    if themes:
        lines.append("\n*Coaching themes*")
        for theme in themes:
            lines.append(f"   - {theme}")

    return "\n".join(lines)


# --------------------------------------------------------------- public API

async def for_rep(rep_email: str) -> dict[str, Any]:
    """Compute this week's scorecard for a single rep."""
    rep_email = (rep_email or "").strip().lower()
    if not rep_email:
        return {"text": "Usage: `@oo sales-reps scorecard <rep_email>`", "error": "empty_rep"}

    week_start, week_end = _window(_WEEK_WINDOW_DAYS)
    trend_start, _ = _window(_TREND_WINDOW_DAYS)

    try:
        week_rows = [_as_summary(r) for r in _query_grades(rep_email, week_start, week_end)]
        trend_rows = [_as_summary(r) for r in _query_grades(rep_email, trend_start, week_end)]
    except Exception as e:  # noqa: BLE001 — DB hiccup shouldn't crash the specialist
        log.exception("scorecard: query failed for %s", rep_email)
        return {
            "text": f"Scorecard for {rep_email}: query failed ({type(e).__name__}).",
            "rep_email": rep_email,
            "error": str(e),
        }

    week_avg = _avg(week_rows)
    trend_avg = _avg(trend_rows)
    best, worst = _best_worst(week_rows)
    themes = _coaching_themes(week_rows)
    text_out = _render(rep_email, week_rows, week_avg, trend_avg, best, worst, themes)

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_scorecard",
        target=f"rep:{rep_email}",
        after={
            "calls_graded": len(week_rows),
            "week_avg": week_avg,
            "trend_avg": trend_avg,
            "critical_miss_calls": sum(1 for r in week_rows if r.critical_misses),
        },
    )

    return {
        "text": text_out,
        "rep_email": rep_email,
        "calls_graded": len(week_rows),
        "week_avg_pct": week_avg,
        "trend_avg_pct": trend_avg,
        "best": {
            "meeting_id": best.meeting_id, "percentage": best.percentage,
            "call_type": best.call_type,
        } if best else None,
        "worst": {
            "meeting_id": worst.meeting_id, "percentage": worst.percentage,
            "call_type": worst.call_type,
        } if worst else None,
        "coaching_themes": themes,
        "critical_miss_calls": sum(1 for r in week_rows if r.critical_misses),
    }
