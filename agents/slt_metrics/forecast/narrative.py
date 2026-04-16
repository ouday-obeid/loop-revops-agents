"""On-demand forecast narrative composer.

Produces a Slack-blocks payload for O's DM when the SLT asks for a forecast
ad-hoc via `@oo slt forecast <quarter>`. Parallel in spirit to
`briefings.daily_830.compose_daily` but quarter-scoped instead of daily.

Narrative composition runs through `ClaudeRouter.narrate("adhoc_slt", ...)`
(Sonnet, see `briefings/narrator.py`). If no API key is present or the call
fails, the router returns the structural fallback — which still carries
`PLACEHOLDER_TAG` while scoring is sparse, so consumers (O → SLT) know the
commit / best / weighted numbers are first-gen until `forecast.scorer`
backfills scores across the snapshot.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from agents.slt_metrics.briefings.narrator import ClaudeRouter
from agents.slt_metrics.forecast import commit_best
from agents.slt_metrics.pipeline import snapshotter
from agents.slt_metrics.types import ForecastRollup, ScoredDeal


PLACEHOLDER_TAG = "(placeholder narrative — pending forecast/narrative.py)"

_ADHOC_SLT_SYSTEM = (
    "You are Agent 6 (SLT Revenue Metrics) for Loop AI. "
    "The SLT just asked for an ad-hoc forecast read. "
    "Produce a 3-4 sentence narrative for Henry (CRO) and Anand (CEO): "
    "lead with commit vs. best-case vs. weighted, then the most material "
    "movement or risk across the scored deals. "
    "If scoring is sparse or the snapshot is empty, caveat openly. "
    "Plain text, no markdown, no emojis."
)


# Quarter tokens we accept. `Q2` implies current fiscal year (Loop AI FY ==
# calendar year per scoping doc). `FY2026-Q2` is the canonical form.
_CANONICAL_Q_RE = re.compile(r"^FY(\d{4})-Q([1-4])$", re.IGNORECASE)
_SHORT_Q_RE = re.compile(r"^Q([1-4])$", re.IGNORECASE)
_RELATIVE_TOKENS = {"this_quarter", "next_quarter"}


class InvalidQuarter(ValueError):
    """Raised when the quarter arg doesn't parse."""


@dataclass(frozen=True)
class QuarterRef:
    """Canonical quarter reference — `FY{year}-Q{q}`."""
    fy_year: int
    quarter: int

    @property
    def label(self) -> str:
        return f"FY{self.fy_year}-Q{self.quarter}"


def parse_quarter(arg: str, *, today: date | None = None) -> QuarterRef:
    """Parse an SLT-supplied quarter token into a QuarterRef.

    Accepts:
      * `FY2026-Q2`  — canonical
      * `Q2`         — current fiscal year
      * `this_quarter` / `next_quarter` — relative to `today`
    """
    today = today or date.today()
    arg_stripped = arg.strip()
    if not arg_stripped:
        raise InvalidQuarter("empty quarter argument")

    token = arg_stripped.lower().replace("-", "_")

    if token in _RELATIVE_TOKENS:
        q = (today.month - 1) // 3 + 1
        year = today.year
        if token == "next_quarter":
            q += 1
            if q > 4:
                q = 1
                year += 1
        return QuarterRef(fy_year=year, quarter=q)

    m = _CANONICAL_Q_RE.match(arg_stripped)
    if m:
        fy = int(m.group(1))
        q = int(m.group(2))
        return QuarterRef(fy_year=fy, quarter=q)

    m = _SHORT_Q_RE.match(arg_stripped)
    if m:
        return QuarterRef(fy_year=today.year, quarter=int(m.group(1)))

    raise InvalidQuarter(
        f"Unrecognized quarter `{arg_stripped}`. "
        "Use `Q2`, `FY2026-Q2`, `this_quarter`, or `next_quarter`."
    )


# ------------------------------------------------------------------ composition

@dataclass(frozen=True)
class ForecastDraftContext:
    """Narrow context the forecast narrative needs.

    Kept parallel to `DailyBriefingContext` / `FridayReviewContext` so a
    future swap to `RevenueModelPayload`-driven composition is trivial.
    """
    as_of: date
    quarter: QuarterRef
    rollup: ForecastRollup
    scored_deals: list[ScoredDeal]
    snapshot_date: date | None
    row_count: int
    placeholder: bool


def _horizon_quarter(d: date) -> str:
    q = (d.month - 1) // 3 + 1
    return f"FY{d.year}-Q{q}"


def _latest_snapshot_context(quarter: QuarterRef, *, today: date) -> ForecastDraftContext:
    """Pull the most-recent snapshot (<= today) and build a draft context.

    If no snapshot exists at all (fresh install, cron hasn't fired yet), we
    return an empty rollup + placeholder=True so callers can still DM O a
    "no data yet" draft rather than silently dropping the request.
    """
    snap_date = snapshotter.latest_snapshot_date(before=today + timedelta(days=1))
    if snap_date is None:
        empty = ForecastRollup(
            horizon_quarter=quarter.label,
            commit_amount=0.0,
            best_case_amount=0.0,
            weighted_amount=0.0,
            deal_count=0,
        )
        return ForecastDraftContext(
            as_of=today,
            quarter=quarter,
            rollup=empty,
            scored_deals=[],
            snapshot_date=None,
            row_count=0,
            placeholder=True,
        )

    rows = snapshotter.read_snapshot(snap_date)
    # Avoid a circular import: jobs.py imports this module lazily in real use,
    # and its `_row_to_scored_deal` is the canonical hydrator.
    from agents.slt_metrics.jobs import _row_to_scored_deal
    deals = [_row_to_scored_deal(r) for r in rows]
    rollup = commit_best.roll_up(deals, horizon_quarter=quarter.label)

    # Phase 1 state: most snapshots are unscored (score=0). Flag placeholder
    # until at least half the rows carry a real score — keeps the consumer
    # honest about what they're looking at.
    scored = sum(1 for d in deals if d.score > 0)
    placeholder = scored < max(1, len(deals) // 2)

    return ForecastDraftContext(
        as_of=today,
        quarter=quarter,
        rollup=rollup,
        scored_deals=deals,
        snapshot_date=snap_date,
        row_count=len(rows),
        placeholder=placeholder,
    )


def build_context(
    quarter_arg: str,
    *,
    today: date | None = None,
) -> ForecastDraftContext:
    """Top-level: parse the arg and hydrate a draft context from latest snapshot."""
    today = today or date.today()
    q = parse_quarter(quarter_arg, today=today)
    return _latest_snapshot_context(q, today=today)


# ------------------------------------------------------------------ blocks

def _headline_block(ctx: ForecastDraftContext) -> dict[str, Any]:
    r = ctx.rollup
    snap_line = (
        f"Based on snapshot `{ctx.snapshot_date.isoformat()}` ({ctx.row_count} deals)"
        if ctx.snapshot_date
        else "_No snapshot available yet — morning cron has not populated data._"
    )
    lines = [
        f"*SLT Forecast Draft · {ctx.quarter.label} · as of {ctx.as_of.isoformat()}*",
        snap_line,
        f"Commit *${r.commit_amount:,.0f}*  ·  Best Case *${r.best_case_amount:,.0f}*  ·  Weighted *${r.weighted_amount:,.0f}*",
        f"Deal count: *{r.deal_count}*",
    ]
    return {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}


def _top_movers_block(ctx: ForecastDraftContext) -> dict[str, Any]:
    top = sorted(
        ctx.scored_deals,
        key=lambda d: d.weighted_acv,
        reverse=True,
    )[:5]
    if not top:
        body = "_No deals in current snapshot._"
    else:
        rows = [
            f"• *{d.opp_name}* ({d.owner_name or '?'}) — "
            f"stage `{d.stage}` · ACV ${(d.acv or 0):,.0f} · weighted ${d.weighted_acv:,.0f}"
            for d in top
        ]
        body = "*Top weighted deals*\n" + "\n".join(rows)
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _stale_block(ctx: ForecastDraftContext) -> dict[str, Any]:
    stale = [d for d in ctx.scored_deals if "stale" in d.risk_flags or "no_activity" in d.risk_flags]
    if not stale:
        body = "*Stale deals:* _none flagged in current snapshot._"
    else:
        top = sorted(stale, key=lambda d: d.weighted_acv, reverse=True)[:5]
        rows = [
            f"• *{d.opp_name}* ({d.owner_name or '?'}) — {', '.join(d.risk_flags) or '—'} · ACV ${(d.acv or 0):,.0f}"
            for d in top
        ]
        body = "*Stale deals*\n" + "\n".join(rows)
    return {"type": "section", "text": {"type": "mrkdwn", "text": body}}


def _narrative_block(ctx: ForecastDraftContext, *, router: ClaudeRouter) -> dict[str, Any]:
    """Compose the narrative via `ClaudeRouter.narrate("adhoc_slt", ...)`.

    The structural fallback (used when no API key / API error) preserves
    `PLACEHOLDER_TAG` whenever scoring is sparse. When Claude does write
    prose but the snapshot is still first-gen, we append the tag so the
    honesty signal survives.
    """
    r = ctx.rollup
    if ctx.row_count == 0:
        fallback = (
            f"No pipeline data available for {ctx.quarter.label}. "
            "The morning snapshot cron may not have run yet, or the DB is empty. "
            f"{PLACEHOLDER_TAG}"
        )
    elif ctx.placeholder:
        fallback = (
            f"Rollup for {ctx.quarter.label} reflects {ctx.row_count} deals from "
            f"the latest snapshot ({ctx.snapshot_date}). Most rows are UNSCORED — "
            "commit / best-case numbers are structural rollups only and will tighten "
            f"once the scoring pass lands. {PLACEHOLDER_TAG}"
        )
    else:
        fallback = (
            f"Rollup for {ctx.quarter.label}: ${r.commit_amount:,.0f} commit, "
            f"${r.best_case_amount:,.0f} best case, ${r.weighted_amount:,.0f} weighted "
            f"across {r.deal_count} deals."
        )

    top = max(ctx.scored_deals, key=lambda d: d.weighted_acv, default=None)
    top_str = ""
    if top is not None:
        top_str = (
            f"Top weighted deal: {top.opp_name} ({top.owner_name or '?'}, "
            f"stage {top.stage}, ACV ${(top.acv or 0):,.0f})."
        )
    stale_count = sum(
        1 for d in ctx.scored_deals
        if "stale" in d.risk_flags or "no_activity" in d.risk_flags
    )
    snap_str = (
        f"latest snapshot {ctx.snapshot_date.isoformat()} ({ctx.row_count} deals)"
        if ctx.snapshot_date
        else "no snapshot available"
    )
    prompt = (
        f"Ad-hoc forecast for {ctx.quarter.label}, as of {ctx.as_of.isoformat()}, "
        f"{snap_str}. "
        f"Commit ${r.commit_amount:,.0f}, best case ${r.best_case_amount:,.0f}, "
        f"weighted ${r.weighted_amount:,.0f} across {r.deal_count} deals. "
        f"{top_str} "
        f"{stale_count} deals flagged stale or inactive. "
        f"Placeholder/unscored: {ctx.placeholder}. "
        "Write the ad-hoc forecast narrative."
    )

    narrative_text = router.narrate(
        "adhoc_slt",
        system=_ADHOC_SLT_SYSTEM,
        user=prompt,
        fallback=fallback,
    )
    if ctx.placeholder and PLACEHOLDER_TAG not in narrative_text:
        narrative_text = f"{narrative_text} {PLACEHOLDER_TAG}"

    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*Narrative*\n{narrative_text}"}}


# ------------------------------------------------------------------ entry point

def compose_forecast_draft(
    ctx: ForecastDraftContext,
    *,
    router: ClaudeRouter | None = None,
) -> dict[str, Any]:
    """Build the forecast draft payload. Returns `{"text": ..., "blocks": [...]}`."""
    router = router or ClaudeRouter()
    blocks: list[dict[str, Any]] = [
        _headline_block(ctx),
        {"type": "divider"},
        _top_movers_block(ctx),
        _stale_block(ctx),
        {"type": "divider"},
        _narrative_block(ctx, router=router),
    ]
    summary = (
        f"SLT forecast draft {ctx.quarter.label} · as of {ctx.as_of.isoformat()} · "
        f"commit ${ctx.rollup.commit_amount:,.0f}"
    )
    return {"text": summary, "blocks": blocks}
