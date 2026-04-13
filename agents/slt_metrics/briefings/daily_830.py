"""Daily 8:30 AM briefing composer.

Produces a Slack-blocks payload for O's DM covering:

1. Headline — as-of date, quarter, commit vs best-case.
2. Top 5 movers since yesterday (by |ΔACV|).
3. Risk watch — deals with flags from the last scoring pass.
4. Pipeline coverage — MM / ENT ratios vs 3x / 4x targets.
5. Generated narrative (Sonnet) synthesizing the above.

Callers pass in a `RevenueModelPayload` (or a narrower DailyBriefingContext)
and receive `{"text": str, "blocks": list[dict]}` ready for the approval
gate workflow.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from agents.slt_metrics.briefings.narrator import ClaudeRouter
from agents.slt_metrics.types import (
    BoardMetrics,
    ForecastRollup,
    MoverSet,
    RevenueModelPayload,
    ScoredDeal,
)


_DAILY_SYSTEM = (
    "You are Agent 6 (SLT Revenue Metrics) for Loop AI. "
    "Produce a concise 3-4 sentence morning briefing for Henry (CRO) and Anand (CEO). "
    "Lead with the forecast headline, then the most material deal movement or risk. "
    "Plain text, no markdown, no emojis."
)


@dataclass(frozen=True)
class DailyBriefingContext:
    """Narrow context the briefing needs — lets tests skip the full payload."""
    as_of: date
    horizon_quarter: str
    rollup: ForecastRollup
    movers: MoverSet
    scored_deals: list[ScoredDeal]
    board_metrics: BoardMetrics
    workbook_url: str | None = None


def context_from_payload(payload: RevenueModelPayload, *, workbook_url: str | None = None) -> DailyBriefingContext:
    return DailyBriefingContext(
        as_of=payload.run_date,
        horizon_quarter=payload.horizon_quarter,
        rollup=payload.forecast_rollup,
        movers=payload.movers,
        scored_deals=payload.scored_deals,
        board_metrics=payload.board_metrics,
        workbook_url=workbook_url,
    )


# ------------------------------------------------------------------ sections

def _headline_block(ctx: DailyBriefingContext) -> dict[str, Any]:
    r = ctx.rollup
    lines = [
        f"*SLT Briefing · {ctx.as_of.isoformat()} · {ctx.horizon_quarter}*",
        f"Commit *${r.commit_amount:,.0f}*  ·  Best Case *${r.best_case_amount:,.0f}*  ·  Weighted *${r.weighted_amount:,.0f}*",
        f"Deal count: *{r.deal_count}*",
    ]
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _movers_block(ctx: DailyBriefingContext) -> dict[str, Any]:
    top = ctx.movers.top(n=5)
    if not top:
        body = "_No material deal movement in the last 24h._"
    else:
        rows = []
        for m in top:
            delta = f"${m.delta_acv:,.0f}" if m.delta_acv is not None else "—"
            rows.append(f"• *{m.opp_name}* ({m.owner_name or '?'}) — {m.kind} · Δ {delta}")
        body = "*Top movers*\n" + "\n".join(rows)
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _risk_watch_block(ctx: DailyBriefingContext) -> dict[str, Any]:
    flagged = [d for d in ctx.scored_deals if d.risk_flags]
    flagged.sort(key=lambda d: d.weighted_acv, reverse=True)
    if not flagged:
        body = "_No flagged risks on commit deals._"
    else:
        top = flagged[:5]
        rows = []
        for d in top:
            flags = ", ".join(d.risk_flags)
            rows.append(f"• *{d.opp_name}* ({d.owner_name or '?'}) — {flags} · ACV ${(d.acv or 0):,.0f}")
        body = "*Risk watch*\n" + "\n".join(rows)
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _coverage_block(ctx: DailyBriefingContext) -> dict[str, Any]:
    mm = ctx.board_metrics.pipeline_coverage_mm
    ent = ctx.board_metrics.pipeline_coverage_ent
    mm_txt = f"{mm:.1f}x" if mm is not None else "—"
    ent_txt = f"{ent:.1f}x" if ent is not None else "—"
    body = (
        "*Pipeline coverage*\n"
        f"• MM: {mm_txt} (target 3.0x)\n"
        f"• ENT: {ent_txt} (target 4.0x)"
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _workbook_block(ctx: DailyBriefingContext) -> dict[str, Any] | None:
    if not ctx.workbook_url:
        return None
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"📎 <{ctx.workbook_url}|Full revenue model workbook>"},
        ],
    }


def _narrative_block(ctx: DailyBriefingContext, *, router: ClaudeRouter) -> dict[str, Any]:
    # Pack a short, dense user prompt — narrator is Sonnet so we can spend a bit.
    top_mover = ctx.movers.top(n=1)
    mover_str = ""
    if top_mover:
        m = top_mover[0]
        delta = f"${m.delta_acv:,.0f}" if m.delta_acv is not None else "—"
        mover_str = f"Top mover: {m.opp_name} ({m.kind}, Δ {delta})."
    prompt = (
        f"As of {ctx.as_of.isoformat()} for {ctx.horizon_quarter}: "
        f"Commit ${ctx.rollup.commit_amount:,.0f}, Best ${ctx.rollup.best_case_amount:,.0f}, "
        f"Weighted ${ctx.rollup.weighted_amount:,.0f} across {ctx.rollup.deal_count} deals. "
        f"{mover_str} "
        f"MM coverage {ctx.board_metrics.pipeline_coverage_mm}, "
        f"ENT coverage {ctx.board_metrics.pipeline_coverage_ent}. "
        "Write the morning briefing."
    )
    fallback = (
        f"Commit ${ctx.rollup.commit_amount:,.0f}; best case ${ctx.rollup.best_case_amount:,.0f}. "
        f"Weighted pipe ${ctx.rollup.weighted_amount:,.0f} across {ctx.rollup.deal_count} deals."
    )
    narrative = router.narrate(
        "daily_briefing",
        system=_DAILY_SYSTEM,
        user=prompt,
        fallback=fallback,
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*Narrative*\n{narrative}"}}


# ------------------------------------------------------------------ entry point

def compose_daily(
    ctx: DailyBriefingContext | RevenueModelPayload,
    *,
    router: ClaudeRouter | None = None,
    workbook_url: str | None = None,
) -> dict[str, Any]:
    """Build the daily briefing payload.

    Accepts either a `DailyBriefingContext` or a `RevenueModelPayload`; the
    latter is unpacked into a context (plus the `workbook_url` override).
    Returns `{"text": str, "blocks": list[dict]}`.
    """
    if isinstance(ctx, RevenueModelPayload):
        ctx = context_from_payload(ctx, workbook_url=workbook_url)

    router = router or ClaudeRouter()

    blocks: list[dict[str, Any]] = [
        _headline_block(ctx),
        {"type": "divider"},
        _movers_block(ctx),
        _risk_watch_block(ctx),
        _coverage_block(ctx),
        {"type": "divider"},
        _narrative_block(ctx, router=router),
    ]
    wb = _workbook_block(ctx)
    if wb is not None:
        blocks.append(wb)

    summary = (
        f"SLT briefing {ctx.as_of.isoformat()} · {ctx.horizon_quarter} · "
        f"commit ${ctx.rollup.commit_amount:,.0f}"
    )
    return {"text": summary, "blocks": blocks}
