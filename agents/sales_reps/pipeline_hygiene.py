"""Pipeline hygiene — daily SF sweep for open-opp hygiene issues.

Rules (all read-only):
  - stale_activity: no activity on an open opp in > STALE_DAYS (default 14)
  - missing_next_step: advanced stage + Next_Step__c empty
  - past_close: CloseDate < today and stage is still open
  - single_threaded: proposal/negotiation stage + OnlyOneContactRole signal

Output: Slack-renderable summary grouped by AE, with a top-N preview per issue
and a persisted audit row. No writes to SF; this surface reports findings and
lets AEs self-correct.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from shared import governance
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"

# Open stages we care about — Closed Won / Closed Lost are excluded.
_OPEN_STAGES = (
    "Prospecting", "Qualification", "Needs Analysis",
    "Value Proposition", "Demo", "Proposal", "Negotiation",
)

# Stages that expect a next-step defined (later-funnel).
_ADVANCED_STAGES = ("Proposal", "Negotiation", "Demo", "Value Proposition")


@dataclass
class HygieneFinding:
    opportunity_id: str
    name: str
    owner_email: str | None
    stage: str
    amount: float | None
    close_date: str | None
    issue: str        # one of: stale_activity, missing_next_step, past_close, single_threaded
    details: str      # short evidence line


@dataclass
class HygieneReport:
    generated_at: str
    ae_filter: str | None
    findings_by_ae: dict[str, list[HygieneFinding]] = field(default_factory=dict)
    totals_by_issue: dict[str, int] = field(default_factory=dict)

    @property
    def total_findings(self) -> int:
        return sum(len(v) for v in self.findings_by_ae.values())


# --------------------------------------------------------------- SOQL builders

def _escape(val: str) -> str:
    return val.replace("'", "\\'")


def _stages_in_clause(stages: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{_escape(s)}'" for s in stages) + ")"


def _soql_stale(ae_filter: str | None, stale_days: int) -> str:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).date().isoformat()
    clauses = [
        f"StageName IN {_stages_in_clause(_OPEN_STAGES)}",
        f"LastActivityDate < {cutoff}",
        "IsClosed = false",
    ]
    if ae_filter:
        clauses.append(f"Owner.Email = '{_escape(ae_filter)}'")
    where = " AND ".join(clauses)
    return (
        "SELECT Id, Name, StageName, Amount, CloseDate, LastActivityDate, "
        "Owner.Email, Owner.Name "
        f"FROM Opportunity WHERE {where} ORDER BY Owner.Email, CloseDate"
    )


def _soql_missing_next_step(ae_filter: str | None) -> str:
    clauses = [
        f"StageName IN {_stages_in_clause(_ADVANCED_STAGES)}",
        "(NextStep = null OR NextStep = '')",
        "IsClosed = false",
    ]
    if ae_filter:
        clauses.append(f"Owner.Email = '{_escape(ae_filter)}'")
    where = " AND ".join(clauses)
    return (
        "SELECT Id, Name, StageName, Amount, CloseDate, NextStep, "
        "Owner.Email, Owner.Name "
        f"FROM Opportunity WHERE {where} ORDER BY Owner.Email, CloseDate"
    )


def _soql_past_close(ae_filter: str | None) -> str:
    today = date.today().isoformat()
    clauses = [
        f"StageName IN {_stages_in_clause(_OPEN_STAGES)}",
        f"CloseDate < {today}",
        "IsClosed = false",
    ]
    if ae_filter:
        clauses.append(f"Owner.Email = '{_escape(ae_filter)}'")
    where = " AND ".join(clauses)
    return (
        "SELECT Id, Name, StageName, Amount, CloseDate, "
        "Owner.Email, Owner.Name "
        f"FROM Opportunity WHERE {where} ORDER BY Owner.Email, CloseDate"
    )


def _soql_single_threaded(ae_filter: str | None) -> str:
    """Late-funnel opps with exactly one external contact role."""
    clauses = [
        f"StageName IN {_stages_in_clause(_ADVANCED_STAGES)}",
        "IsClosed = false",
    ]
    if ae_filter:
        clauses.append(f"Owner.Email = '{_escape(ae_filter)}'")
    where = " AND ".join(clauses)
    return (
        "SELECT Id, Name, StageName, Amount, CloseDate, Owner.Email, Owner.Name, "
        "(SELECT Id FROM OpportunityContactRoles) "
        f"FROM Opportunity WHERE {where} ORDER BY Owner.Email, CloseDate"
    )


# --------------------------------------------------------------- findings

def _row_owner_email(row: dict[str, Any]) -> str | None:
    owner = row.get("Owner") or {}
    return (owner.get("Email") or "").lower() or None


def _find_stale(ae_filter: str | None, stale_days: int) -> list[HygieneFinding]:
    result = salesforce_mcp.soql_query(_soql_stale(ae_filter, stale_days), limit=500)
    out: list[HygieneFinding] = []
    for r in result.get("records", []) or []:
        out.append(HygieneFinding(
            opportunity_id=r["Id"],
            name=r.get("Name", ""),
            owner_email=_row_owner_email(r),
            stage=r.get("StageName", ""),
            amount=r.get("Amount"),
            close_date=r.get("CloseDate"),
            issue="stale_activity",
            details=f"Last activity {r.get('LastActivityDate') or 'never'} (>{stale_days}d)",
        ))
    return out


def _find_missing_next_step(ae_filter: str | None) -> list[HygieneFinding]:
    result = salesforce_mcp.soql_query(_soql_missing_next_step(ae_filter), limit=500)
    out: list[HygieneFinding] = []
    for r in result.get("records", []) or []:
        out.append(HygieneFinding(
            opportunity_id=r["Id"],
            name=r.get("Name", ""),
            owner_email=_row_owner_email(r),
            stage=r.get("StageName", ""),
            amount=r.get("Amount"),
            close_date=r.get("CloseDate"),
            issue="missing_next_step",
            details=f"Stage {r.get('StageName','?')} with no NextStep",
        ))
    return out


def _find_past_close(ae_filter: str | None) -> list[HygieneFinding]:
    result = salesforce_mcp.soql_query(_soql_past_close(ae_filter), limit=500)
    out: list[HygieneFinding] = []
    for r in result.get("records", []) or []:
        out.append(HygieneFinding(
            opportunity_id=r["Id"],
            name=r.get("Name", ""),
            owner_email=_row_owner_email(r),
            stage=r.get("StageName", ""),
            amount=r.get("Amount"),
            close_date=r.get("CloseDate"),
            issue="past_close",
            details=f"Close date {r.get('CloseDate')} is past",
        ))
    return out


def _find_single_threaded(ae_filter: str | None) -> list[HygieneFinding]:
    result = salesforce_mcp.soql_query(_soql_single_threaded(ae_filter), limit=500)
    out: list[HygieneFinding] = []
    for r in result.get("records", []) or []:
        roles = (r.get("OpportunityContactRoles") or {}).get("records") or []
        if len(roles) <= 1:
            out.append(HygieneFinding(
                opportunity_id=r["Id"],
                name=r.get("Name", ""),
                owner_email=_row_owner_email(r),
                stage=r.get("StageName", ""),
                amount=r.get("Amount"),
                close_date=r.get("CloseDate"),
                issue="single_threaded",
                details=f"Only {len(roles)} contact role(s) on opp",
            ))
    return out


# --------------------------------------------------------------- aggregation

def _aggregate(findings: list[HygieneFinding], ae_filter: str | None) -> HygieneReport:
    report = HygieneReport(
        generated_at=datetime.now(timezone.utc).isoformat(),
        ae_filter=ae_filter,
    )
    for f in findings:
        key = f.owner_email or "(unassigned)"
        report.findings_by_ae.setdefault(key, []).append(f)
        report.totals_by_issue[f.issue] = report.totals_by_issue.get(f.issue, 0) + 1
    return report


# --------------------------------------------------------------- Slack rendering

def _render_slack(report: HygieneReport, preview_per_ae: int = 5) -> str:
    if report.total_findings == 0:
        ae_note = f" for {report.ae_filter}" if report.ae_filter else ""
        return f"*Pipeline hygiene*{ae_note}: no issues found ✓"

    lines: list[str] = []
    scope = f"AE: {report.ae_filter}" if report.ae_filter else "all AEs"
    lines.append(f"*Pipeline hygiene — {scope}*")
    lines.append(
        "Totals: " + " · ".join(
            f"{k}={v}" for k, v in sorted(report.totals_by_issue.items())
        )
    )

    for ae_email, items in sorted(report.findings_by_ae.items(),
                                  key=lambda kv: -len(kv[1])):
        lines.append(f"\n• *{ae_email}* ({len(items)})")
        for f in items[:preview_per_ae]:
            amount = f"${f.amount:,.0f}" if f.amount else "—"
            lines.append(
                f"   - `{f.opportunity_id}` {f.name[:40]} · {f.stage} · "
                f"{amount} · close {f.close_date or '?'} · "
                f"_{f.issue}_ {f.details}"
            )
        if len(items) > preview_per_ae:
            lines.append(f"   …and {len(items) - preview_per_ae} more")
    return "\n".join(lines)


# --------------------------------------------------------------- public API

async def run(
    ae_filter: str | None = None,
    *,
    stale_days: int = 14,
    preview_per_ae: int = 5,
) -> dict[str, Any]:
    """Execute the daily hygiene sweep. Returns Slack payload + full report."""
    findings: list[HygieneFinding] = []
    try:
        findings.extend(_find_stale(ae_filter, stale_days))
        findings.extend(_find_missing_next_step(ae_filter))
        findings.extend(_find_past_close(ae_filter))
        findings.extend(_find_single_threaded(ae_filter))
    except Exception as e:  # noqa: BLE001 — daily sweep must degrade, not crash the agent
        log.exception("hygiene sweep failed")
        return {
            "text": f"sales_reps: hygiene sweep failed — {type(e).__name__}: {e}",
            "error": str(e),
        }

    report = _aggregate(findings, ae_filter)
    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_hygiene_sweep",
        target=f"ae:{ae_filter or 'all'}",
        after={"totals": report.totals_by_issue, "total_findings": report.total_findings},
    )
    return {
        "text": _render_slack(report, preview_per_ae=preview_per_ae),
        "ae_filter": ae_filter,
        "total_findings": report.total_findings,
        "totals_by_issue": report.totals_by_issue,
        "findings_by_ae": {
            k: [
                {
                    "opportunity_id": f.opportunity_id,
                    "name": f.name,
                    "stage": f.stage,
                    "amount": f.amount,
                    "close_date": f.close_date,
                    "issue": f.issue,
                    "details": f.details,
                }
                for f in v
            ]
            for k, v in report.findings_by_ae.items()
        },
    }
