"""Read-only formatters for @oo cs <subcommand>.

Pulls from `cs_account_health`, `cs_churn_risk`, `cs_renewal_state`, and
`tasks` — all owned by this agent. No SF calls (those belong in the sweeps).
Returns markdown strings ready for Slack.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from shared.db.connection import get_engine


def _engine():
    return get_engine()


def _find_account_row(account: str) -> dict | None:
    """Accept either SF account id or (case-insensitive) name substring."""
    engine = _engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT account_id, name, score, nps_score, nps_category,
                          nps_at, last_touch_at, checked_at
                     FROM cs_account_health
                    WHERE account_id = :q
                       OR LOWER(name) LIKE :like
                 ORDER BY checked_at DESC LIMIT 1"""
            ),
            {"q": account, "like": f"%{account.lower()}%"},
        ).mappings().first()
    return dict(row) if row else None


def account_status(account: str) -> str:
    row = _find_account_row(account)
    if not row:
        return f"No CS record found for `{account}`."

    acct_id = row["account_id"]
    engine = _engine()
    with engine.begin() as conn:
        risk = conn.execute(
            text(
                """SELECT score, tier, created_at FROM cs_churn_risk
                    WHERE account_id = :a
                 ORDER BY created_at DESC LIMIT 1"""
            ),
            {"a": acct_id},
        ).mappings().first()
        renewal = conn.execute(
            text(
                """SELECT opportunity_id, stage, contract_end_date, provisional
                     FROM cs_renewal_state
                    WHERE account_id = :a
                 ORDER BY created_at DESC LIMIT 1"""
            ),
            {"a": acct_id},
        ).mappings().first()
        open_tasks = conn.execute(
            text(
                """SELECT COUNT(*) FROM tasks
                    WHERE agent_name = 'cs' AND status != 'completed'
                      AND source LIKE :s"""
            ),
            {"s": f"cs:%{acct_id}%"},
        ).scalar() or 0

    nps_suffix = f" ({row['nps_score']})" if row['nps_score'] is not None else ""
    lines = [
        f"# {row['name'] or acct_id} — `{acct_id}`",
        f"- Vitally health: **{_fmt_score(row['score'])}**  ·  "
        f"NPS: {row['nps_category'] or 'unknown'}{nps_suffix}",
        f"- Last touch: {_fmt_ts(row['last_touch_at'])}  ·  "
        f"checked {_fmt_ts(row['checked_at'])}",
    ]
    if risk:
        lines.append(
            f"- Churn risk: score **{risk['score']}** (tier {risk['tier']}) "
            f"as of {_fmt_ts(risk['created_at'])}"
        )
    else:
        lines.append("- Churn risk: _not scored yet_")
    if renewal:
        prov = " _(provisional)_" if renewal["provisional"] else ""
        lines.append(
            f"- Renewal opp `{renewal['opportunity_id']}`: "
            f"stage **{renewal['stage']}**, ends {renewal['contract_end_date']}{prov}"
        )
    else:
        lines.append("- Renewal: _no opp tracked_")
    lines.append(f"- Open CS tasks: {int(open_tasks)}")
    return "\n".join(lines)


def account_health_trend(account: str, window_days: int = 30) -> str:
    row = _find_account_row(account)
    if not row:
        return f"No CS record found for `{account}`."
    acct_id = row["account_id"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    engine = _engine()
    with engine.begin() as conn:
        history = conn.execute(
            text(
                """SELECT score, checked_at FROM cs_account_health_history
                    WHERE account_id = :a AND checked_at >= :c
                 ORDER BY checked_at ASC"""
            ),
            {"a": acct_id, "c": cutoff},
        ).mappings().all()

    current = row["score"]
    nps_suffix = f" ({row['nps_score']})" if row['nps_score'] is not None else ""
    lines = [
        f"# Health — {row['name'] or acct_id}",
        f"- Current Vitally score: **{_fmt_score(current)}**",
        f"- NPS: {row['nps_category'] or 'unknown'}{nps_suffix}  ·  "
        f"last survey {_fmt_ts(row['nps_at'])}",
    ]
    if history:
        first = history[0]["score"]
        last = history[-1]["score"]
        delta = (last - first) if (first is not None and last is not None) else None
        if delta is not None:
            direction = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
            lines.append(
                f"- {window_days}d trend: {direction} {abs(delta):.0f} points "
                f"({_fmt_score(first)} → {_fmt_score(last)}, {len(history)} samples)"
            )
        else:
            lines.append(f"- {window_days}d trend: {len(history)} samples, partial data")
    else:
        lines.append(f"- {window_days}d trend: _no history rows yet_")
    return "\n".join(lines)


def renewals_overview() -> str:
    engine = _engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        pipeline = conn.execute(
            text(
                """SELECT opportunity_id, account_id, stage, contract_end_date,
                          provisional
                     FROM cs_renewal_state
                    WHERE contract_end_date >= :today
                 ORDER BY contract_end_date ASC LIMIT 20"""
            ),
            {"today": now.date().isoformat()},
        ).mappings().all()
        stalls = conn.execute(
            text(
                """SELECT title, source, priority, created_at FROM tasks
                    WHERE category = 'renewal_stall' AND status != 'completed'
                 ORDER BY created_at DESC LIMIT 20"""
            )
        ).mappings().all()

    lines = ["# Renewal pipeline"]
    if not pipeline:
        lines.append("- _No upcoming renewal opps tracked._")
    else:
        for p in pipeline:
            prov = " _(provisional)_" if p["provisional"] else ""
            lines.append(
                f"- `{p['opportunity_id']}` — {p['account_id']} · "
                f"**{p['stage']}** · ends {p['contract_end_date']}{prov}"
            )
    lines.append("")
    lines.append("## Open stalls")
    if not stalls:
        lines.append("- _None._")
    else:
        for s in stalls:
            lines.append(
                f"- [{s['priority']}] {s['title']}  _(source: {s['source']})_"
            )
    return "\n".join(lines)


def churn_risk_list(tier: int | None = None, limit: int = 20) -> str:
    engine = _engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT r.account_id, r.score, r.tier, r.factors_json,
                          r.created_at, h.name
                     FROM cs_churn_risk r
                LEFT JOIN cs_account_health h ON h.account_id = r.account_id
                    WHERE r.tier >= :t
                 ORDER BY r.score DESC LIMIT :n"""
            ),
            {"t": tier or 50, "n": limit},
        ).mappings().all()

    header = f"# Churn risk (tier ≥{tier or 50})"
    if not rows:
        return header + "\n- _No accounts at or above this tier._"
    lines = [header]
    for r in rows:
        try:
            contribs = json.loads(r["factors_json"]).get("contributions", {})
        except (TypeError, ValueError):
            contribs = {}
        top = sorted(contribs.items(), key=lambda kv: kv[1], reverse=True)[:3]
        factors = ", ".join(f"`{k}`={v:.0f}" for k, v in top if v > 0) or "n/a"
        lines.append(
            f"- **{r['name'] or r['account_id']}** (`{r['account_id']}`) — "
            f"score {r['score']} (tier {r['tier']}) · {factors}"
        )
    return "\n".join(lines)


def _fmt_ts(ts) -> str:
    if ts is None:
        return "never"
    if isinstance(ts, str):
        return ts[:16]
    return ts.isoformat(sep=" ")[:16]


def _fmt_score(score) -> str:
    if score is None:
        return "n/a"
    return f"{int(round(score))}"
