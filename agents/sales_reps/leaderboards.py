"""Weekly SDR + AE leaderboards.

Runs Friday 16:00 ET by scheduler. Two kinds:
  - ae: pipeline created + closed won + avg call grade, ranked by $.
  - sdr: meetings booked + cold-call grade + conversion, ranked by meetings.

Data sources:
  - `sales_reps_call_grades` — avg grade, # calls, critical miss count.
  - Salesforce — Opportunity aggregates by Owner.Email.

Week window is ISO-week (Monday 00:00 UTC → Sunday 23:59:59 UTC).
Rendering is Slack-ready; callers post where they want (Hutch DM, channel).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from sqlalchemy import text

from shared import governance
from shared.db.connection import get_engine
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"
LeaderKind = Literal["ae", "sdr"]


@dataclass
class LeaderRow:
    rep_email: str
    rep_name: str | None
    calls_graded: int
    avg_grade_pct: float | None
    critical_misses: int
    # AE-only
    pipeline_created: float | None = None
    closed_won: float | None = None
    # SDR-only
    meetings_booked: int | None = None


# --------------------------------------------------------------- week math

def iso_week_bounds(week: str | None = None) -> tuple[datetime, datetime, str]:
    """Return (start_utc, end_utc, label) for an ISO week.

    week=None → current week. week="2026-W15" → that specific week.
    end is EXCLUSIVE (Monday 00:00 UTC of the following week).
    """
    if week:
        try:
            year_str, week_str = week.split("-W")
            iso_year, iso_week = int(year_str), int(week_str)
        except ValueError as e:
            raise ValueError(f"bad week format (want YYYY-WNN): {week}") from e
    else:
        iso_year, iso_week, _ = datetime.now(timezone.utc).isocalendar()

    start = datetime.fromisocalendar(iso_year, iso_week, 1).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=7)
    label = f"{iso_year}-W{iso_week:02d}"
    return start, end, label


# --------------------------------------------------------------- grades

def _grade_stats_by_rep(
    start: datetime, end: datetime, *, call_type_filter: tuple[str, ...] | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate call grades per rep for the given window.

    Returns rep_email → {calls_graded, avg_grade_pct, critical_misses, rep_name}.
    """
    where = ["graded_at >= :s", "graded_at < :e", "rep_email IS NOT NULL"]
    params: dict[str, Any] = {"s": start, "e": end}
    if call_type_filter:
        placeholders = ",".join(f":ct{i}" for i in range(len(call_type_filter)))
        where.append(f"call_type IN ({placeholders})")
        for i, ct in enumerate(call_type_filter):
            params[f"ct{i}"] = ct

    q = (
        "SELECT rep_email, rep_name, "
        "       COUNT(*) AS n, "
        "       AVG(percentage) AS avg_pct, "
        "       SUM(CASE WHEN critical_misses IS NOT NULL "
        "                AND critical_misses != '[]' THEN 1 ELSE 0 END) AS crit_miss "
        "FROM sales_reps_call_grades "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY rep_email, rep_name"
    )
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(text(q), params).mappings().all()
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        email = r["rep_email"]
        out[email] = {
            "rep_email": email,
            "rep_name": r.get("rep_name"),
            "calls_graded": int(r["n"] or 0),
            "avg_grade_pct": float(r["avg_pct"]) if r["avg_pct"] is not None else None,
            "critical_misses": int(r["crit_miss"] or 0),
        }
    return out


# --------------------------------------------------------------- SF aggregates

def _pipeline_by_owner(start: datetime, end: datetime) -> dict[str, dict[str, float]]:
    """Pipeline created + closed won this week, keyed by Owner.Email."""
    start_iso = start.date().isoformat()
    end_iso = end.date().isoformat()
    q = (
        "SELECT Owner.Email, Owner.Name, StageName, Amount, CreatedDate, CloseDate, IsClosed, IsWon "
        f"FROM Opportunity WHERE (CreatedDate >= {start_iso}T00:00:00Z "
        f"AND CreatedDate < {end_iso}T00:00:00Z) "
        f"OR (IsClosed = true AND CloseDate >= {start_iso} AND CloseDate < {end_iso})"
    )
    try:
        rows = salesforce_mcp.soql_query(q, limit=2000).get("records", []) or []
    except Exception as e:  # noqa: BLE001 — SF down shouldn't block the leaderboard entirely
        log.warning("leaderboard: pipeline SOQL failed: %s", e)
        return {}

    out: dict[str, dict[str, float]] = {}
    for r in rows:
        email = ((r.get("Owner") or {}).get("Email") or "").lower()
        if not email:
            continue
        bucket = out.setdefault(email, {
            "pipeline_created": 0.0,
            "closed_won": 0.0,
            "owner_name": (r.get("Owner") or {}).get("Name"),
        })
        amt = float(r.get("Amount") or 0)
        created = r.get("CreatedDate", "")
        if created and start_iso <= created[:10] < end_iso:
            bucket["pipeline_created"] += amt
        if r.get("IsWon"):
            bucket["closed_won"] += amt
    return out


def _sdr_meetings_by_owner(start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
    """Event records created in-window keyed by SDR owner email.

    We treat Event + Subject LIKE '%demo%' as a booked demo — Loop's convention.
    """
    start_iso = start.date().isoformat()
    end_iso = end.date().isoformat()
    q = (
        "SELECT Owner.Email, Owner.Name, Subject, CreatedDate "
        f"FROM Event WHERE CreatedDate >= {start_iso}T00:00:00Z "
        f"AND CreatedDate < {end_iso}T00:00:00Z"
    )
    try:
        rows = salesforce_mcp.soql_query(q, limit=2000).get("records", []) or []
    except Exception as e:  # noqa: BLE001
        log.warning("leaderboard: SDR meetings SOQL failed: %s", e)
        return {}

    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        email = ((r.get("Owner") or {}).get("Email") or "").lower()
        if not email:
            continue
        subj = (r.get("Subject") or "").lower()
        if "demo" not in subj and "discovery" not in subj:
            continue
        bucket = out.setdefault(email, {"meetings_booked": 0, "owner_name": (r.get("Owner") or {}).get("Name")})
        bucket["meetings_booked"] += 1
    return out


# --------------------------------------------------------------- assembly

def _ae_rows(start: datetime, end: datetime) -> list[LeaderRow]:
    grades = _grade_stats_by_rep(start, end)
    pipeline = _pipeline_by_owner(start, end)

    emails = set(grades) | set(pipeline)
    rows: list[LeaderRow] = []
    for email in emails:
        g = grades.get(email, {})
        p = pipeline.get(email, {})
        rows.append(LeaderRow(
            rep_email=email,
            rep_name=g.get("rep_name") or p.get("owner_name"),
            calls_graded=int(g.get("calls_graded", 0)),
            avg_grade_pct=g.get("avg_grade_pct"),
            critical_misses=int(g.get("critical_misses", 0)),
            pipeline_created=float(p.get("pipeline_created", 0.0)),
            closed_won=float(p.get("closed_won", 0.0)),
        ))
    # Rank by: closed_won desc, then pipeline_created desc, then avg_grade_pct desc.
    rows.sort(
        key=lambda r: (
            -(r.closed_won or 0),
            -(r.pipeline_created or 0),
            -(r.avg_grade_pct or 0),
        )
    )
    return rows


def _sdr_rows(start: datetime, end: datetime) -> list[LeaderRow]:
    grades = _grade_stats_by_rep(start, end, call_type_filter=("sdr_cold_call",))
    meetings = _sdr_meetings_by_owner(start, end)

    emails = set(grades) | set(meetings)
    rows: list[LeaderRow] = []
    for email in emails:
        g = grades.get(email, {})
        m = meetings.get(email, {})
        rows.append(LeaderRow(
            rep_email=email,
            rep_name=g.get("rep_name") or m.get("owner_name"),
            calls_graded=int(g.get("calls_graded", 0)),
            avg_grade_pct=g.get("avg_grade_pct"),
            critical_misses=int(g.get("critical_misses", 0)),
            meetings_booked=int(m.get("meetings_booked", 0)),
        ))
    rows.sort(
        key=lambda r: (
            -(r.meetings_booked or 0),
            -(r.avg_grade_pct or 0),
        )
    )
    return rows


# --------------------------------------------------------------- rendering

def _render(kind: LeaderKind, label: str, rows: list[LeaderRow]) -> str:
    if not rows:
        return f"*{kind.upper()} leaderboard — {label}*\n  _no activity this week_"

    header = f"*{kind.upper()} leaderboard — {label}*  ·  {len(rows)} reps"
    lines = [header]

    if kind == "ae":
        lines.append("  rank · rep · closed-won · pipeline · calls · avg grade")
        for i, r in enumerate(rows, start=1):
            grade = f"{r.avg_grade_pct:.0f}%" if r.avg_grade_pct is not None else "—"
            lines.append(
                f"   {i:>2}. {r.rep_name or r.rep_email} · "
                f"${(r.closed_won or 0):,.0f} · "
                f"${(r.pipeline_created or 0):,.0f} · "
                f"{r.calls_graded} · {grade}"
                + (f" · ⚠️ {r.critical_misses} crit" if r.critical_misses else "")
            )
    else:
        lines.append("  rank · rep · demos booked · cold-calls · avg grade")
        for i, r in enumerate(rows, start=1):
            grade = f"{r.avg_grade_pct:.0f}%" if r.avg_grade_pct is not None else "—"
            lines.append(
                f"   {i:>2}. {r.rep_name or r.rep_email} · "
                f"{r.meetings_booked or 0} · "
                f"{r.calls_graded} · {grade}"
                + (f" · ⚠️ {r.critical_misses} crit" if r.critical_misses else "")
            )
    return "\n".join(lines)


# --------------------------------------------------------------- public API

async def snapshot(kind: str = "ae", week: str | None = None) -> dict[str, Any]:
    """Compute the leaderboard for `kind` and `week` (None = current)."""
    if kind not in ("ae", "sdr"):
        return {
            "text": "Usage: `@oo sales-reps leaderboard [ae|sdr] [week]`",
            "error": "bad_kind",
            "kind": kind,
        }
    try:
        start, end, label = iso_week_bounds(week)
    except ValueError as e:
        return {"text": f"Bad week format: {e}", "error": "bad_week"}

    rows = _ae_rows(start, end) if kind == "ae" else _sdr_rows(start, end)
    text_out = _render(kind, label, rows)

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_leaderboard",
        target=f"leaderboard:{kind}:{label}",
        after={"kind": kind, "week": label, "rep_count": len(rows)},
    )

    return {
        "text": text_out,
        "kind": kind,
        "week": label,
        "rows": [
            {
                "rep_email": r.rep_email,
                "rep_name": r.rep_name,
                "calls_graded": r.calls_graded,
                "avg_grade_pct": r.avg_grade_pct,
                "critical_misses": r.critical_misses,
                "pipeline_created": r.pipeline_created,
                "closed_won": r.closed_won,
                "meetings_booked": r.meetings_booked,
            }
            for r in rows
        ],
    }
