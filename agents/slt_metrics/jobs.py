"""Top-level callables for SLT Metrics scheduled jobs.

Registered in `shared/runtime/schedule.py` and invoked by launchd / Cloud
Scheduler. Each function is synchronous at the boundary so the schedule
runner doesn't need an event loop — async bits live inside.

Phase 1 state: `run_morning_snapshot` writes an UNSCORED snapshot because
forecast scoring lands in D6. When scoring is wired, swap the call to
`write_snapshot(score_all(opps), ...)` — nothing else in the schedule
changes.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable

from agents.slt_metrics.board_metrics.arr_nrr import ArrNrrSnapshot
from agents.slt_metrics.board_metrics.board_summary import build_board_metrics
from agents.slt_metrics.board_metrics.pipeline_coverage import build_coverage_report
from agents.slt_metrics.board_metrics.unit_economics import build_unit_economics
from agents.slt_metrics.briefings.daily_830 import compose_daily
from agents.slt_metrics.briefings.friday_review import compose_friday
from agents.slt_metrics.briefings.narrator import ClaudeRouter
from agents.slt_metrics.forecast import commit_best
from agents.slt_metrics.pipeline import fetcher, movers, snapshotter
from agents.slt_metrics.types import (
    ForecastWeights,
    PillarScore,
    RevenueModelPayload,
    ScoredDeal,
)

log = logging.getLogger(__name__)


# O's Slack DM — routing destination for every SLT draft (decision locked
# 2026-04-13). Slack accepts user IDs as channel IDs for DMs.
_O_DM_CHANNEL = "U07P4GX9YLQ"

SenderFn = Callable[[str, str, list[dict[str, Any]] | None], dict[str, Any]]


# ------------------------------------------------------------------ morning snapshot

def run_morning_snapshot() -> dict[str, int]:
    """Fetch open opps + write today's pipeline_snapshots row per opp.

    Returns `{"fetched": N, "inserted": M}`. `M` can be 0 on a rerun — the
    snapshotter logs a warning in that case.
    """
    today = date.today()
    log.info("run_morning_snapshot: starting for %s", today)
    opps = fetcher.fetch_open_opps()
    inserted = snapshotter.write_unscored_snapshot(opps, snapshot_date=today)
    log.info(
        "run_morning_snapshot: fetched=%d inserted=%d date=%s",
        len(opps), inserted, today,
    )
    return {"fetched": len(opps), "inserted": inserted}


# ------------------------------------------------------------------ briefings

def run_daily_briefing(
    *,
    today: date | None = None,
    sender: SenderFn | None = None,
    router: ClaudeRouter | None = None,
    workbook_url: str | None = None,
) -> dict[str, Any]:
    """Compose daily briefing → slt_draft_review gate → O's DM.

    The briefing draft lands in O's DM only; O forwards manually (decision
    locked 2026-04-13, no auto-channel fanout).
    """
    return _run_briefing(
        kind="daily",
        today=today,
        sender=sender,
        router=router,
        workbook_url=workbook_url,
        period_days=1,
    )


def run_friday_review(
    *,
    today: date | None = None,
    sender: SenderFn | None = None,
    router: ClaudeRouter | None = None,
    workbook_url: str | None = None,
) -> dict[str, Any]:
    """Compose Friday weekly review → slt_draft_review gate → O's DM.

    Looks back 7 days for the movers comparison window.
    """
    return _run_briefing(
        kind="friday",
        today=today,
        sender=sender,
        router=router,
        workbook_url=workbook_url,
        period_days=7,
    )


# ------------------------------------------------------------------ internals

def _run_briefing(
    *,
    kind: str,
    today: date | None,
    sender: SenderFn | None,
    router: ClaudeRouter | None,
    workbook_url: str | None,
    period_days: int,
) -> dict[str, Any]:
    today = today or date.today()

    curr_date, curr_rows = _resolve_current_snapshot(today)
    if not curr_rows:
        log.warning("run_%s_briefing: no snapshot rows available (today=%s)", kind, today)
        return {"status": "no_data", "run_date": today.isoformat(), "deals": 0}

    candidate_prev = snapshotter.latest_snapshot_date(before=curr_date)
    lookback_floor = curr_date - timedelta(days=period_days)
    prev_rows: list[dict[str, Any]] = []
    prev_date: date | None = None
    if candidate_prev is not None and candidate_prev >= lookback_floor:
        prev_date = candidate_prev
        prev_rows = snapshotter.read_snapshot(prev_date)

    payload = _build_payload(
        run_date=curr_date,
        curr_rows=curr_rows,
        prev_rows=prev_rows,
        prev_date=prev_date,
    )
    router = router or ClaudeRouter()

    if kind == "daily":
        briefing = compose_daily(payload, router=router, workbook_url=workbook_url)
        header = "SLT daily briefing draft"
    else:
        briefing = compose_friday(payload, router=router, workbook_url=workbook_url)
        header = "SLT Friday review draft"

    gate_id = _create_gate(kind=kind, run_date=curr_date, summary=briefing["text"])
    full_blocks = _approval_wrapper_blocks(gate_id, header, briefing["text"]) + briefing["blocks"]

    send = sender or _default_sender
    send_result = send(_O_DM_CHANNEL, briefing["text"], full_blocks)

    log.info(
        "run_%s_briefing: gate=%s date=%s deals=%d movers=%d",
        kind, gate_id, curr_date, len(payload.scored_deals), len(payload.movers.movers),
    )
    return {
        "status": "sent",
        "kind": kind,
        "run_date": curr_date.isoformat(),
        "prev_date": prev_date.isoformat() if prev_date else None,
        "gate_id": gate_id,
        "deals": len(payload.scored_deals),
        "movers": len(payload.movers.movers),
        "slack_ok": bool(send_result.get("ok")) if isinstance(send_result, dict) else False,
    }


def _resolve_current_snapshot(today: date) -> tuple[date, list[dict[str, Any]]]:
    """Prefer today's snapshot; fall back to the most-recent one if cron hasn't
    fired yet today (weekend reruns, manual backfills)."""
    rows = snapshotter.read_snapshot(today)
    if rows:
        return today, rows
    fallback = snapshotter.latest_snapshot_date(before=today + timedelta(days=1))
    if fallback is None:
        return today, []
    return fallback, snapshotter.read_snapshot(fallback)


def _build_payload(
    *,
    run_date: date,
    curr_rows: list[dict[str, Any]],
    prev_rows: list[dict[str, Any]],
    prev_date: date | None,
) -> RevenueModelPayload:
    scored = [_row_to_scored_deal(r) for r in curr_rows]
    horizon = _horizon_quarter(run_date)
    rollup = commit_best.roll_up(scored, horizon_quarter=horizon)
    moverset = movers.diff(
        prev_rows or None,
        curr_rows,
        period_from=prev_date or run_date,
        period_to=run_date,
    )

    # Unit economics deferred to BigQuery swap-in — ship gap-flagged.
    ue = build_unit_economics(None)
    coverage = build_coverage_report(scored_deals=scored, quotas_by_segment={})
    arr_nrr = ArrNrrSnapshot(
        as_of=run_date, arr=None, nrr=None, logo_retention=None, expansion_rate=None,
    )
    board = build_board_metrics(
        as_of=run_date, arr_nrr=arr_nrr, coverage=coverage, unit_economics=ue,
    )
    return RevenueModelPayload(
        run_date=run_date,
        horizon_quarter=horizon,
        weights=ForecastWeights(),
        scored_deals=scored,
        forecast_rollup=rollup,
        movers=moverset,
        ae_cards=[],
        sdr_cards=[],
        board_metrics=board,
    )


def _row_to_scored_deal(row: dict[str, Any]) -> ScoredDeal:
    """Rehydrate a `pipeline_snapshots` row into a ScoredDeal.

    Unscored rows (score=None) render as zeros with an empty pillar map so the
    rollup/movers/briefing still work pre-D6. Once the morning chain switches
    to `write_snapshot(score_all(opps), ...)`, these rows carry real numbers
    and the rehydration is loss-less for everything the briefing reads.
    """
    meta = row.get("metadata") or {}
    if not isinstance(meta, dict):
        meta = {}
    raw_pillars = meta.get("pillars") or {}
    pillars = {
        name: PillarScore(value=float(info.get("value", 0.0) or 0.0), detail=str(info.get("detail", "")))
        for name, info in raw_pillars.items()
        if isinstance(info, dict)
    }
    sf_raw = meta.get("sf_raw") or {}
    opp_name = sf_raw.get("Name") if isinstance(sf_raw, dict) else None
    account_name = None
    if isinstance(sf_raw, dict):
        acct = sf_raw.get("Account") or {}
        if isinstance(acct, dict):
            account_name = acct.get("Name")

    close_date = row.get("close_date")
    if isinstance(close_date, str):
        try:
            close_date = date.fromisoformat(close_date[:10])
        except ValueError:
            close_date = None

    return ScoredDeal(
        opp_id=row.get("opp_id", ""),
        opp_name=opp_name or row.get("opp_id", ""),
        owner_name=row.get("owner_name"),
        account_name=account_name,
        segment=row.get("segment"),
        stage=row.get("stage") or "",
        amount=_to_float(row.get("amount")),
        acv=_to_float(row.get("acv")),
        close_date=close_date if isinstance(close_date, date) else None,
        score=int(row.get("score") or 0),
        probability=float(row.get("probability") or 0.0),
        category=row.get("category") or "Pipe Dream",
        weighted_acv=float(row.get("weighted_acv") or 0.0),
        pillars=pillars,
        risk_flags=list(meta.get("risk_flags") or []),
        weights_version=str(meta.get("weights_version") or "unscored"),
        raw=None,
    )


def _horizon_quarter(d: date) -> str:
    """Loop AI fiscal year == calendar year (per scoping doc). FYYYYY-Q#."""
    q = (d.month - 1) // 3 + 1
    return f"FY{d.year}-Q{q}"


def _create_gate(*, kind: str, run_date: date, summary: str) -> int:
    from shared.governance import create_approval_gate

    return create_approval_gate(
        agent_name="slt_metrics",
        action_type="slt_draft_review",
        payload={
            "kind": kind,
            "run_date": run_date.isoformat(),
            "summary": summary,
        },
        justification=None,
        requested_by="cron:slt_metrics",
    )


def _approval_wrapper_blocks(
    gate_id: int, header: str, summary: str
) -> list[dict[str, Any]]:
    from shared.slack_dispatcher import approval_blocks

    return approval_blocks(gate_id, header, summary)


def _default_sender(
    channel: str, text_: str, blocks: list[dict[str, Any]] | None
) -> dict[str, Any]:
    from shared.slack_dispatcher import SlackSender

    return SlackSender().send(channel, text_, blocks)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
