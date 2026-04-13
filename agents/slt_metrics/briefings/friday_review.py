"""Friday 4 PM Review composer.

Weekly wrap-up: quarter progress, week-over-week movement, top AE/SDR,
coverage trend, and an Opus-generated narrative. Delivered to O's DM for
manual forward to the SLT channel.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from agents.slt_metrics.briefings.narrator import ClaudeRouter
from agents.slt_metrics.types import (
    AeCard,
    BoardMetrics,
    ForecastRollup,
    MoverSet,
    RevenueModelPayload,
    ScoredDeal,
    SdrCard,
)


_FRIDAY_SYSTEM = (
    "You are Agent 6 (SLT Revenue Metrics) for Loop AI. "
    "Produce a 5-6 sentence weekly wrap for Henry (CRO), Anand (CEO), and Hutch (VP Sales). "
    "Structure: quarter progress, weekly movement, top performer, biggest risk, "
    "coverage read, one forward-looking observation. Plain text, no markdown, no emojis."
)


@dataclass(frozen=True)
class FridayReviewContext:
    as_of: date
    horizon_quarter: str
    rollup: ForecastRollup
    movers: MoverSet
    scored_deals: list[ScoredDeal]
    ae_cards: list[AeCard]
    sdr_cards: list[SdrCard]
    board_metrics: BoardMetrics
    workbook_url: str | None = None


def context_from_payload(payload: RevenueModelPayload, *, workbook_url: str | None = None) -> FridayReviewContext:
    return FridayReviewContext(
        as_of=payload.run_date,
        horizon_quarter=payload.horizon_quarter,
        rollup=payload.forecast_rollup,
        movers=payload.movers,
        scored_deals=payload.scored_deals,
        ae_cards=payload.ae_cards,
        sdr_cards=payload.sdr_cards,
        board_metrics=payload.board_metrics,
        workbook_url=workbook_url,
    )


# ------------------------------------------------------------------ sections

def _headline_block(ctx: FridayReviewContext) -> dict[str, Any]:
    r = ctx.rollup
    lines = [
        f"*Friday Review · {ctx.as_of.isoformat()} · {ctx.horizon_quarter}*",
        f"Commit *${r.commit_amount:,.0f}*  ·  Best *${r.best_case_amount:,.0f}*  ·  Weighted *${r.weighted_amount:,.0f}*",
    ]
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _top_ae_block(ctx: FridayReviewContext) -> dict[str, Any]:
    eligible = [c for c in ctx.ae_cards if c.attainment_pct is not None]
    if not eligible:
        body = "*Top AE:* —"
    else:
        top = max(eligible, key=lambda c: c.attainment_pct or 0)
        body = (
            f"*Top AE:* {top.rep_name or top.rep_email} — "
            f"attainment {(top.attainment_pct or 0):.0%}, commit deals {top.deals_commit}"
        )
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _top_sdr_block(ctx: FridayReviewContext) -> dict[str, Any]:
    if not ctx.sdr_cards:
        body = "*Top SDR:* —"
    else:
        top = max(ctx.sdr_cards, key=lambda c: c.pipeline_sourced)
        body = (
            f"*Top SDR:* {top.sdr_name or top.sdr_email} — "
            f"sourced ${top.pipeline_sourced:,.0f}, held {top.meetings_held}"
        )
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _weekly_movers_block(ctx: FridayReviewContext) -> dict[str, Any]:
    top = ctx.movers.top(n=5)
    if not top:
        body = "*Weekly movers:* _none material_"
    else:
        rows = []
        for m in top:
            delta = f"${m.delta_acv:,.0f}" if m.delta_acv is not None else "—"
            rows.append(f"• *{m.opp_name}* ({m.owner_name or '?'}) — {m.kind} · Δ {delta}")
        body = "*Weekly movers*\n" + "\n".join(rows)
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _risk_block(ctx: FridayReviewContext) -> dict[str, Any]:
    flagged = sorted(
        (d for d in ctx.scored_deals if d.risk_flags),
        key=lambda d: d.weighted_acv, reverse=True,
    )[:5]
    if not flagged:
        body = "*Risk watch:* _no flagged commit deals_"
    else:
        rows = [
            f"• *{d.opp_name}* — {', '.join(d.risk_flags)} · ACV ${(d.acv or 0):,.0f}"
            for d in flagged
        ]
        body = "*Risk watch*\n" + "\n".join(rows)
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _coverage_block(ctx: FridayReviewContext) -> dict[str, Any]:
    mm = ctx.board_metrics.pipeline_coverage_mm
    ent = ctx.board_metrics.pipeline_coverage_ent
    mm_txt = f"{mm:.1f}x" if mm is not None else "—"
    ent_txt = f"{ent:.1f}x" if ent is not None else "—"
    body = f"*Coverage:* MM {mm_txt} (target 3.0x) · ENT {ent_txt} (target 4.0x)"
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _workbook_block(ctx: FridayReviewContext) -> dict[str, Any] | None:
    if not ctx.workbook_url:
        return None
    return {
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"📎 <{ctx.workbook_url}|Full revenue model workbook>"},
        ],
    }


def _narrative_block(ctx: FridayReviewContext, *, router: ClaudeRouter) -> dict[str, Any]:
    flagged = sum(1 for d in ctx.scored_deals if d.risk_flags)
    top_mover = ctx.movers.top(n=1)
    mover_str = ""
    if top_mover:
        m = top_mover[0]
        delta = f"${m.delta_acv:,.0f}" if m.delta_acv is not None else "—"
        mover_str = f"Top weekly mover: {m.opp_name} ({m.kind}, Δ {delta})."

    prompt = (
        f"Week ending {ctx.as_of.isoformat()}, quarter {ctx.horizon_quarter}. "
        f"Commit ${ctx.rollup.commit_amount:,.0f}, best ${ctx.rollup.best_case_amount:,.0f}, "
        f"weighted ${ctx.rollup.weighted_amount:,.0f} on {ctx.rollup.deal_count} deals. "
        f"{mover_str} "
        f"{flagged} deals flagged with risks. "
        f"MM coverage {ctx.board_metrics.pipeline_coverage_mm}, "
        f"ENT coverage {ctx.board_metrics.pipeline_coverage_ent}. "
        "Write the Friday review."
    )
    fallback = (
        f"Week close for {ctx.horizon_quarter}: commit ${ctx.rollup.commit_amount:,.0f}, "
        f"best ${ctx.rollup.best_case_amount:,.0f}. {ctx.rollup.deal_count} deals in play, "
        f"{flagged} flagged."
    )
    narrative = router.narrate(
        "friday_wrap",
        system=_FRIDAY_SYSTEM,
        user=prompt,
        fallback=fallback,
    )
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*Weekly narrative*\n{narrative}"}}


# ------------------------------------------------------------------ entry point

def compose_friday(
    ctx: FridayReviewContext | RevenueModelPayload,
    *,
    router: ClaudeRouter | None = None,
    workbook_url: str | None = None,
) -> dict[str, Any]:
    if isinstance(ctx, RevenueModelPayload):
        ctx = context_from_payload(ctx, workbook_url=workbook_url)

    router = router or ClaudeRouter()

    blocks: list[dict[str, Any]] = [
        _headline_block(ctx),
        {"type": "divider"},
        _top_ae_block(ctx),
        _top_sdr_block(ctx),
        _weekly_movers_block(ctx),
        _risk_block(ctx),
        _coverage_block(ctx),
        {"type": "divider"},
        _narrative_block(ctx, router=router),
    ]
    wb = _workbook_block(ctx)
    if wb is not None:
        blocks.append(wb)

    summary = (
        f"Friday review {ctx.as_of.isoformat()} · {ctx.horizon_quarter} · "
        f"commit ${ctx.rollup.commit_amount:,.0f}"
    )
    return {"text": summary, "blocks": blocks}
