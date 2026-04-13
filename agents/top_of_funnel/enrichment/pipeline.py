"""Enrichment pipeline orchestrator.

Cron entry: `run_pipeline()` fires 02:00 Mon–Fri. Flow:

  (1) Apollo search               — returns candidate accounts
  (2) For each account (Semaphore(8)):
        Apollo people_lookup      — decision-maker titles
        Clay enrichment           — Grade A/B only (budget + floor enforced)
        Suppression check         — 5-layer read-only
        ICP score                 — 100pt model
        SF dedup probe            — Lead.Email / Contact.Email / Account.Website
        Buffer to tof_lead_candidates  (status='ready' ; sf_lead_id=None)
  (3) One approval gate per run
        count < 2            → no gate needed (single_record_update auto_notify)
        2–99                 → bulk_update_small (slack_button)
        100+                 → bulk_update_large (slack_explicit + justification)
  (4) create_lead per buffered row, carrying gate_id

Writes one `tof_enrichment_runs` row per invocation. Respects
`sf_lead_creation_daily=200` rate limit — hitting the cap marks the run
`partial` and logs the remainder for tomorrow.

Slack entry: `@oo tof enrich <domain>` — runs the same flow for a single
domain, bypassing the cron gate (dry-run by default; writes require
`@oo tof enrich <domain> write`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from agents.top_of_funnel import routing, suppression
from agents.top_of_funnel.enrichment import apollo_client, clay_client
from agents.top_of_funnel.icp_scorer import score_account
from agents.top_of_funnel.state import get_state_engine

log = logging.getLogger(__name__)

_AGENT_NAME = "top_of_funnel"
_PARALLEL = 8
_SMALL_GATE_THRESHOLD = 100
_TARGET_DECISION_TITLES = (
    "Director of Operations",
    "VP Operations",
    "Chief Operating Officer",
    "Franchise Owner",
    "CFO",
    "CEO",
)


# ----------------------------------------------------------------- dataclasses


@dataclass
class EnrichedCandidate:
    domain: str
    company_name: str | None
    email: str | None
    first_name: str | None
    last_name: str | None
    title: str | None
    phone: str | None
    account_payload: dict[str, Any]
    icp_score: int
    icp_tier: str
    icp_signals: dict[str, int]
    suppressed: bool
    suppression_reason: str = ""
    clay_skipped: bool = False
    clay_skip_reason: str = ""
    error: str | None = None
    # Routing — populated serially after gather (see run_pipeline).
    segment: str = ""
    assigned_sdr_id: str | None = None
    assigned_sdr_email: str | None = None
    assigned_sdr_slack_id: str | None = None


@dataclass
class PipelineReport:
    run_id: str
    started_at: datetime
    completed_at: datetime | None = None
    status: str = "running"         # running|success|partial|error
    scanned: int = 0
    suppressed: int = 0
    scored_a: int = 0
    scored_b: int = 0
    scored_c: int = 0
    scored_d: int = 0
    buffered: int = 0
    written: int = 0
    dedup_skipped: int = 0
    clay_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    approval_gate_id: int | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "scanned": self.scanned,
            "suppressed": self.suppressed,
            "tiers": {
                "A": self.scored_a, "B": self.scored_b,
                "C": self.scored_c, "D": self.scored_d,
            },
            "buffered": self.buffered,
            "written": self.written,
            "dedup_skipped": self.dedup_skipped,
            "clay_skipped": self.clay_skipped,
            "approval_gate_id": self.approval_gate_id,
            "dry_run": self.dry_run,
            "errors": self.errors,
        }


# --------------------------------------------------------------- single-account


async def _enrich_one_account(
    account: dict[str, Any],
    *,
    clay_budget: clay_client.CreditBudget,
    grade_floor: str,
    sf_query: Callable[[str], dict[str, Any]] | None,
    http_client: Any,
) -> EnrichedCandidate:
    """Apollo people → Clay → suppression → ICP score. Pure enrichment;
    writes to tof_lead_candidates happen in the batch loop."""
    domain = (account.get("domain") or "").lower()
    company = account.get("name")

    # People lookup (Apollo) — soft-fails to [].
    people = await apollo_client.people_lookup(
        domain=domain,
        titles=list(_TARGET_DECISION_TITLES),
        limit=3,
        http_client=http_client,
    )

    person = people[0] if people else None
    first = (person.first_name if person else None) or None
    last = (person.last_name if person else None) or None
    title = (person.title if person else None) or None
    email = (person.email if person else None) or None

    # Clay enrichment for verified email + phone, gated by grade floor.
    # Apollo's "grade" isn't standard; we carry "B" as a sensible default so
    # Grade-B-or-higher floor allows enrichment of reasonably-trusted rows.
    clay_res = None
    clay_grade = account.get("apollo_grade") or "B"
    try:
        clay_res = await clay_client.enrich_contact(
            domain=domain,
            first_name=first,
            last_name=last,
            title=title,
            grade=clay_grade,
            budget=clay_budget,
            grade_floor=grade_floor,
        )
    except clay_client.ClayBudgetExceeded as exc:
        log.warning("clay_budget_exceeded: %s", exc)
        # Continue without Clay enrichment — Apollo-only data is still useful.
        clay_res = clay_client.ClayEnrichResult(
            email=email, phone=None, grade=clay_grade, credits_used=0,
            skipped=True, skip_reason=f"budget_exceeded: {exc}",
        )

    final_email = clay_res.email or email
    final_phone = clay_res.phone

    # Suppression — if we have an email, check it; otherwise treat as not-suppressed.
    supp = suppression.SuppressionResult(False, "", "none")
    if final_email:
        supp = await suppression.is_suppressed(
            final_email,
            domain=domain,
            sf_query=sf_query,
        )

    # ICP score — run regardless so C/D rows are visible for exploration slot.
    score_payload = dict(account)
    score_payload.setdefault("domain", domain)
    score = score_account(score_payload)

    return EnrichedCandidate(
        domain=domain,
        company_name=company,
        email=final_email,
        first_name=first,
        last_name=last,
        title=title,
        phone=final_phone,
        account_payload=score_payload,
        icp_score=score.total,
        icp_tier=score.tier,
        icp_signals=score.signals,
        suppressed=supp.suppressed,
        suppression_reason=supp.reason if supp.suppressed else "",
        clay_skipped=bool(clay_res.skipped),
        clay_skip_reason=clay_res.skip_reason or "",
    )


# ---------------------------------------------------------------- buffer write


def _buffer_candidate(run_id: str, cand: EnrichedCandidate) -> None:
    """Persist one enriched candidate to tof_lead_candidates with status='ready'
    (or 'suppressed' if the suppression layer flagged it)."""
    status = "suppressed" if cand.suppressed else "ready"
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_lead_candidates (
                     run_id, domain, company_name, email, first_name, last_name,
                     title, phone, location_count, brand, ownership_type,
                     icp_score, icp_tier, icp_signals_json, account_payload, status,
                     error_message, assigned_sdr_id
                   ) VALUES (
                     :run_id, :domain, :company, :email, :first, :last,
                     :title, :phone, :loc, :brand, :own,
                     :score, :tier, :signals, :payload, :status, :err, :sdr
                   )"""
            ),
            {
                "run_id": run_id,
                "domain": cand.domain,
                "company": cand.company_name,
                "email": cand.email,
                "first": cand.first_name,
                "last": cand.last_name,
                "title": cand.title,
                "phone": cand.phone,
                "loc": cand.account_payload.get("location_count"),
                "brand": cand.account_payload.get("brand"),
                "own": cand.account_payload.get("ownership_type"),
                "score": cand.icp_score,
                "tier": cand.icp_tier,
                "signals": json.dumps(cand.icp_signals),
                "payload": json.dumps(cand.account_payload),
                "status": status,
                "err": cand.suppression_reason or cand.error or None,
                "sdr": cand.assigned_sdr_id,
            },
        )


# ----------------------------------------------------------------- run record


def _open_run(report: PipelineReport) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_enrichment_runs (run_id, started_at, status)
                   VALUES (:r, :s, 'running')"""
            ),
            {"r": report.run_id, "s": report.started_at},
        )


def _close_run(report: PipelineReport) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE tof_enrichment_runs
                   SET completed_at = :c, status = :st,
                       scanned = :sc, suppressed = :su,
                       scored_a = :a, scored_b = :b,
                       written_count = :w, errors_json = :e
                   WHERE run_id = :r"""
            ),
            {
                "c": report.completed_at,
                "st": report.status,
                "sc": report.scanned,
                "su": report.suppressed,
                "a": report.scored_a,
                "b": report.scored_b,
                "w": report.written,
                "e": json.dumps(report.errors) if report.errors else None,
                "r": report.run_id,
            },
        )


# ------------------------------------------------------------------- writer


def _writable_candidates(run_id: str) -> list[dict[str, Any]]:
    """Return ready-but-unwritten candidates for the given run."""
    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT id, domain, company_name, email, first_name, last_name,
                          title, phone, icp_score, icp_tier, brand, ownership_type,
                          assigned_sdr_id
                   FROM tof_lead_candidates
                   WHERE run_id = :r AND status = 'ready' AND sf_lead_id IS NULL
                   ORDER BY icp_score DESC"""
            ),
            {"r": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def _mark_written(candidate_id: int, sf_lead_id: str) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE tof_lead_candidates
                   SET sf_lead_id = :sf, status = 'briefed'
                   WHERE id = :id"""
            ),
            {"sf": sf_lead_id, "id": candidate_id},
        )


def _mark_dedup(candidate_id: int, reason: str) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """UPDATE tof_lead_candidates
                   SET status = 'suppressed', error_message = :msg
                   WHERE id = :id"""
            ),
            {"msg": f"dedup:{reason}", "id": candidate_id},
        )


# ------------------------------------------------------------ approval gate


def _create_run_gate(
    *,
    count: int,
    run_id: str,
    create_gate_fn: Callable[..., int] | None,
    justification: str | None = None,
) -> int | None:
    """One gate per run. None for count < 2 (single_record_update is auto_notify)."""
    if count < 2:
        return None
    if create_gate_fn is None:
        from shared.governance import create_approval_gate as _cg
        create_gate_fn = _cg
    action_type = "bulk_update_small" if count < _SMALL_GATE_THRESHOLD else "bulk_update_large"
    payload = {"run_id": run_id, "count": count, "sobject": "Lead"}
    if action_type == "bulk_update_large":
        justification = justification or (
            f"Top-of-funnel pipeline run {run_id}: {count} leads from ICP-qualified "
            f"restaurant franchise groups. Standard daily briefing volume."
        )
    return create_gate_fn(
        agent_name=_AGENT_NAME,
        action_type=action_type,
        payload=payload,
        justification=justification,
    )


# ------------------------------------------------------------------- public


async def run_pipeline(
    *,
    search_filters: dict[str, Any] | None = None,
    grade_floor: str = "B",
    dry_run: bool = False,
    sf_query: Callable[[str], dict[str, Any]] | None = None,
    create_fn: Callable[..., dict[str, Any]] | None = None,
    describe_fn: Callable[[str], dict[str, Any]] | None = None,
    create_gate_fn: Callable[..., int] | None = None,
    clay_budget: clay_client.CreditBudget | None = None,
    http_client: Any = None,
    auto_approve_gate: bool = False,
) -> dict[str, Any]:
    """Full pipeline. Returns the PipelineReport dict."""
    run_id = datetime.now(timezone.utc).strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    report = PipelineReport(
        run_id=run_id,
        started_at=datetime.now(timezone.utc),
        dry_run=dry_run,
    )
    _open_run(report)

    try:
        # (1) Apollo search
        filters = search_filters or {"industry_keywords": ["restaurants"], "per_page": 25}
        accounts = await apollo_client.search_accounts(
            filters=filters,
            http_client=http_client,
        )
        report.scanned = len(accounts)
        if not accounts:
            report.status = "success"
            report.errors.append("apollo_returned_zero")
            report.completed_at = datetime.now(timezone.utc)
            _close_run(report)
            return report.to_dict()

        if clay_budget is None:
            clay_budget = clay_client.CreditBudget.from_env()

        # (2) Parallel enrichment
        sem = asyncio.Semaphore(_PARALLEL)

        async def bounded(acc: Any) -> EnrichedCandidate:
            async with sem:
                try:
                    return await _enrich_one_account(
                        acc.to_dict() if hasattr(acc, "to_dict") else acc,
                        clay_budget=clay_budget,
                        grade_floor=grade_floor,
                        sf_query=sf_query,
                        http_client=http_client,
                    )
                except Exception as exc:  # noqa: BLE001
                    domain = getattr(acc, "domain", None) or (acc.get("domain") if isinstance(acc, dict) else "?")
                    log.exception("enrichment failed for %s", domain)
                    return EnrichedCandidate(
                        domain=domain or "?", company_name=None, email=None,
                        first_name=None, last_name=None, title=None, phone=None,
                        account_payload={"domain": domain},
                        icp_score=0, icp_tier="D", icp_signals={},
                        suppressed=False, error=str(exc),
                    )

        candidates = await asyncio.gather(*(bounded(a) for a in accounts))

        # (2b) Route non-suppressed candidates. Run serially — routing increments
        # per-segment round-robin state in SQLite; parallel would skew fairness.
        # Suppressed rows are skipped so we don't burn rotation slots on them.
        try:
            territory_cfg = routing.load_territory()
        except Exception as exc:  # noqa: BLE001
            territory_cfg = None
            log.warning("routing: territory config unavailable, leads will buffer without SDR: %s", exc)

        for cand in candidates:
            if cand.suppressed or cand.error or territory_cfg is None:
                continue
            try:
                rr = routing.assign_owner(
                    {"location_count": cand.account_payload.get("location_count")},
                    territory_cfg=territory_cfg,
                    auto_refresh_cache=False,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("routing failed for %s: %s", cand.domain, exc)
                continue
            cand.segment = rr.segment
            cand.assigned_sdr_id = rr.sdr_user_id
            cand.assigned_sdr_email = rr.sdr_email
            cand.assigned_sdr_slack_id = rr.sdr_slack_id

        # (3) Buffer + tally
        for cand in candidates:
            _buffer_candidate(run_id, cand)
            if cand.error:
                report.errors.append(f"{cand.domain}: {cand.error}")
            if cand.suppressed:
                report.suppressed += 1
            if cand.clay_skipped:
                report.clay_skipped += 1
            report.buffered += 1
            if cand.icp_tier == "A":
                report.scored_a += 1
            elif cand.icp_tier == "B":
                report.scored_b += 1
            elif cand.icp_tier == "C":
                report.scored_c += 1
            else:
                report.scored_d += 1

        # (4) SF writes — skip in dry-run.
        if dry_run:
            report.status = "success"
            report.completed_at = datetime.now(timezone.utc)
            _close_run(report)
            return report.to_dict()

        writable = _writable_candidates(run_id)
        gate_id = _create_run_gate(
            count=len(writable),
            run_id=run_id,
            create_gate_fn=create_gate_fn,
        )
        report.approval_gate_id = gate_id

        if gate_id is not None and auto_approve_gate:
            # For integration tests only — production uses Slack-button approval.
            from shared.governance import auto_approve_gate as _aa
            _aa(gate_id, approver="system")

        if create_fn is None:
            # Default to the real SF MCP create_record path.
            from agents.top_of_funnel import sf_lead_writer as _writer

            def _default_create(sobject: str, fields: dict[str, Any], **kw: Any) -> dict[str, Any]:
                from shared.mcp.salesforce_mcp import create_record as _mcp_create
                return _mcp_create(sobject, fields, **kw)

            describe_fn_eff = describe_fn
            sf_query_eff = sf_query
            create_record_eff = _default_create
        else:
            from agents.top_of_funnel import sf_lead_writer as _writer
            describe_fn_eff = describe_fn
            sf_query_eff = sf_query
            create_record_eff = create_fn

        for row in writable:
            lead_in = {
                "domain": row["domain"],
                "company_name": row["company_name"],
                "email": row["email"],
                "first_name": row["first_name"],
                "last_name": row["last_name"],
                "title": row["title"],
                "phone": row["phone"],
                "icp_score": row["icp_score"],
                "icp_tier": row["icp_tier"],
                "brand": row["brand"],
                "ownership_type": row["ownership_type"],
                "assigned_sdr_id": row["assigned_sdr_id"],
            }
            try:
                out = _writer.create_lead(
                    lead_in,
                    approval_gate_id=gate_id if gate_id is not None else 0,
                    describe_fn=describe_fn_eff,
                    sf_query=sf_query_eff,
                    create_fn=create_record_eff,
                    skip_dedup=(sf_query_eff is None),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("sf_lead_create failed for %s", row["domain"])
                report.errors.append(f"{row['domain']}: {exc}")
                continue

            if out.get("skipped"):
                report.dedup_skipped += 1
                _mark_dedup(row["id"], out["dedup"]["reason"])
                continue

            sf_id = out.get("sf_id")
            if sf_id:
                _mark_written(row["id"], sf_id)
                report.written += 1
            else:
                report.errors.append(f"{row['domain']}: no_sf_id_in_response")

        report.status = "partial" if report.errors else "success"
        report.completed_at = datetime.now(timezone.utc)
        _close_run(report)
        return report.to_dict()

    except Exception as exc:  # noqa: BLE001
        log.exception("pipeline run %s failed", run_id)
        report.status = "error"
        report.errors.append(str(exc))
        report.completed_at = datetime.now(timezone.utc)
        _close_run(report)
        raise


# ------------------------------------------------------------ Slack entry


async def enrich_single(domain: str, *, write: bool = False) -> dict[str, Any]:
    """`@oo tof enrich <domain>` — runs the flow for one domain.

    Dry-run by default (writes only when `write=True`). Always returns a
    Slack-formattable dict.
    """
    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return {"text": f"`{domain}` doesn't look like a domain."}

    filters = {"q_organization_domains_list": [domain], "per_page": 1}
    result = await run_pipeline(
        search_filters=filters,
        dry_run=not write,
    )
    tiers = result.get("tiers") or {}
    return {
        "text": (
            f"*Enrich `{domain}`* — run `{result['run_id']}`\n"
            f"Scanned: {result['scanned']}  |  Buffered: {result['buffered']}  |  "
            f"Written: {result['written']}  |  Suppressed: {result['suppressed']}\n"
            f"Tiers A/B/C/D: {tiers.get('A',0)}/{tiers.get('B',0)}"
            f"/{tiers.get('C',0)}/{tiers.get('D',0)}"
        ),
        "report": result,
    }
