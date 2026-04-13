"""Weekly CS digest — Monday 07:00 ET.

Sends a single markdown report to Jackie's DM (env `JACKIE_SLACK_USER_ID`) plus
`#agent-cs-log`. Contents:

  - Top 10 churn risks (tier ≥70), with top factor contributions
  - Renewal pipeline summary (created this week, provisional count)
  - Renewal stalls currently open
  - Expansion signals surfaced this week
  - UID match rate (latest from integration_health)
  - NPS freshness rate

Pure computation of the report body is in `build_report()` — tests cover that
surface directly; `send()` is the thin launcher that posts it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config
from shared.slack_dispatcher import SlackSender

log = logging.getLogger(__name__)

CHANNEL = "#agent-cs-log"


def _top_churn_risks(since: datetime, limit: int = 10) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT r.account_id, r.score, r.tier, r.factors_json, h.name
                     FROM cs_churn_risk r
                LEFT JOIN cs_account_health h ON h.account_id = r.account_id
                    WHERE r.created_at >= :s AND r.tier >= 70
                 ORDER BY r.score DESC
                    LIMIT :n"""
            ),
            {"s": since, "n": limit},
        ).mappings().all()
    out = []
    for r in rows:
        try:
            decoded = json.loads(r["factors_json"])
            contribs = decoded.get("contributions", {})
        except (TypeError, ValueError):
            contribs = {}
        top = sorted(contribs.items(), key=lambda kv: kv[1], reverse=True)[:3]
        out.append({
            "account_id": r["account_id"],
            "name": r["name"] or r["account_id"],
            "score": r["score"],
            "tier": r["tier"],
            "top_factors": [(k, v) for k, v in top if v > 0],
        })
    return out


def _renewals_created_since(since: datetime) -> dict[str, int]:
    engine = get_engine()
    with engine.begin() as conn:
        total = conn.execute(
            text("SELECT COUNT(*) FROM cs_renewal_state WHERE created_at >= :s"),
            {"s": since},
        ).scalar() or 0
        provisional = conn.execute(
            text(
                """SELECT COUNT(*) FROM cs_renewal_state
                    WHERE created_at >= :s AND provisional = 1"""
            ),
            {"s": since},
        ).scalar() or 0
    return {"total": int(total), "provisional": int(provisional)}


def _open_stalls() -> int:
    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text(
                """SELECT COUNT(*) FROM tasks
                    WHERE category = 'renewal_stall' AND status != 'completed'"""
            )
        ).scalar() or 0
    return int(count)


def _expansion_signals_since(since: datetime) -> int:
    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(
            text(
                """SELECT COUNT(*) FROM tasks
                    WHERE category = 'expansion' AND created_at >= :s"""
            ),
            {"s": since},
        ).scalar() or 0
    return int(count)


def _latest_uid_match_rate() -> tuple[str, str | None] | None:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT status, error_message FROM integration_health
                    WHERE integration = 'vitally_uid_resolution'
                 ORDER BY checked_at DESC LIMIT 1"""
            )
        ).mappings().first()
    return (row["status"], row["error_message"]) if row else None


def _nps_freshness(now: datetime) -> tuple[int, int]:
    cutoff = now - timedelta(days=30)
    engine = get_engine()
    with engine.begin() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM cs_account_health")).scalar() or 0
        fresh = (
            conn.execute(
                text("SELECT COUNT(*) FROM cs_account_health WHERE nps_at >= :c"),
                {"c": cutoff},
            ).scalar()
            or 0
        )
    return int(fresh), int(total)


def build_report(now: datetime | None = None) -> str:
    """Compose the weekly markdown digest. Pure read — no side effects."""
    now = now or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)

    risks = _top_churn_risks(week_ago)
    renewals = _renewals_created_since(week_ago)
    stalls = _open_stalls()
    expansion = _expansion_signals_since(week_ago)
    uid_rate = _latest_uid_match_rate()
    nps_fresh, nps_total = _nps_freshness(now)

    lines = [
        f"# CS weekly digest — {now.date().isoformat()}",
        "",
        f"_Window:_ {week_ago.date().isoformat()} → {now.date().isoformat()}",
        "",
        "## Top churn risks (tier ≥70)",
    ]
    if not risks:
        lines.append("- _No accounts scored tier ≥70 this week._")
    else:
        for r in risks:
            factors = ", ".join(f"`{k}`={v:.0f}" for k, v in r["top_factors"]) or "n/a"
            lines.append(
                f"- **{r['name']}** (`{r['account_id']}`) — score {r['score']} "
                f"(tier {r['tier']}) · {factors}"
            )
    lines.append("")

    lines.append("## Renewal pipeline")
    lines.append(f"- New renewal opps this week: **{renewals['total']}**")
    if renewals["provisional"]:
        lines.append(
            f"- Provisional (awaiting `Renewal Outreach` stage): {renewals['provisional']}"
        )
    lines.append(f"- Open stalls (≥14d): {stalls}")
    lines.append("")

    lines.append("## Expansion signals")
    lines.append(f"- New expansion tasks this week: **{expansion}**")
    lines.append("")

    lines.append("## Data quality")
    if uid_rate:
        status, err = uid_rate
        rate_desc = err or "≥95% match"
        lines.append(f"- Vitally UID match: **{status}** ({rate_desc})")
    else:
        lines.append("- Vitally UID match: _no poll data yet_")
    if nps_total:
        pct = (nps_fresh / nps_total) * 100
        lines.append(
            f"- NPS freshness (30d): **{pct:.0f}%** ({nps_fresh}/{nps_total} accounts)"
        )
    else:
        lines.append("- NPS freshness: _no account health rows yet_")

    return "\n".join(lines)


async def send(
    *,
    slack_sender: SlackSender | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build + post the weekly digest. Returns send metadata for tests."""
    sender = slack_sender or SlackSender()
    body = build_report(now=now)

    jackie_id = get_config("JACKIE_SLACK_USER_ID")
    targets: list[str] = [CHANNEL]
    if jackie_id and jackie_id != "REPLACE":
        targets.append(jackie_id)

    results = []
    for t in targets:
        results.append(sender.send(t, body))
    return {"posted_to": targets, "results": results}
