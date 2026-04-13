"""Daily churn-risk sweep — tier routing + persistence.

Reads cs_account_health (current Vitally state), joins SF Case counts,
Fireflies last-call date, and Opportunity renewal date. Scores each account
via `risk.scoring.score_account`, persists one row per account per day to
`cs_churn_risk`, and routes alerts:

    tier 50 → log only (surfaces in weekly digest)
    tier 70 → post to #agent-cs-log; create pending task
    tier 85 → post to #agent-cs-log with @Jackie mention; create urgent task

Anti-false-positive guard: tier ≥70 alerts require ≥2 non-zero factors.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.slack_dispatcher import SlackSender

from agents.cs.risk import scoring

log = logging.getLogger(__name__)

ALERT_CHANNEL = "#agent-cs-log"
JACKIE_SLACK_HANDLE = "@jackie"  # placeholder — M9 resolves to real user ID


def _iter_accounts() -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """SELECT account_id, name, score AS current_health, nps_category,
                          nps_at, last_touch_at
                     FROM cs_account_health"""
            )
        ).mappings().all()
    return [dict(r) for r in rows]


def _prev_30d_avg(account_id: str, now: datetime) -> float | None:
    engine = get_engine()
    since = now - timedelta(days=30)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT AVG(score) FROM cs_account_health_history
                    WHERE account_id = :a AND checked_at >= :s AND score IS NOT NULL"""
            ),
            {"a": account_id, "s": since},
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def _case_counts(account_id: str, sf_mcp: Any, now: datetime) -> tuple[int, int]:
    """Returns (cases_last_30d, cases_prior_30d). Best-effort; 0/0 on error."""
    try:
        cutoff_30 = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        cutoff_60 = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        q = (
            f"SELECT COUNT(Id) c FROM Case "
            f"WHERE AccountId = '{account_id}' AND CreatedDate > {cutoff_30}"
        )
        last = sf_mcp.soql_query(q, limit=1)
        q2 = (
            f"SELECT COUNT(Id) c FROM Case "
            f"WHERE AccountId = '{account_id}' "
            f"AND CreatedDate > {cutoff_60} AND CreatedDate <= {cutoff_30}"
        )
        prior = sf_mcp.soql_query(q2, limit=1)
        return _count_from(last), _count_from(prior)
    except Exception as e:
        log.warning("case_counts failed for %s: %s", account_id, e)
        return 0, 0


def _count_from(resp: dict) -> int:
    if not resp:
        return 0
    total = resp.get("totalSize")
    if total:
        return int(total)
    records = resp.get("records") or []
    if records and isinstance(records[0], dict):
        return int(records[0].get("c", 0) or 0)
    return 0


def _renewal_gap(account_id: str, sf_mcp: Any, fireflies_mcp: Any, now: datetime) -> tuple[
    int | None, int | None
]:
    """Returns (days_until_renewal, days_since_last_call)."""
    days_to_renewal: int | None = None
    try:
        q = (
            "SELECT Zen_Contract_End_Date__c FROM Opportunity "
            f"WHERE AccountId = '{account_id}' AND Type = 'Renewal' "
            "AND IsClosed = false ORDER BY Zen_Contract_End_Date__c ASC LIMIT 1"
        )
        r = sf_mcp.soql_query(q, limit=1)
        records = r.get("records") or []
        if records:
            raw = records[0].get("Zen_Contract_End_Date__c")
            if raw:
                end = datetime.fromisoformat(str(raw)).replace(tzinfo=timezone.utc)
                days_to_renewal = (end - now).days
    except Exception as e:
        log.warning("renewal lookup failed for %s: %s", account_id, e)

    days_since_call: int | None = None
    try:
        rows = fireflies_mcp.list_transcripts(account_id=account_id, limit=1)
        if rows:
            raw = (rows[0] or {}).get("date") or (rows[0] or {}).get("dateTime")
            if raw:
                dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                days_since_call = (now - dt).days
    except Exception as e:
        log.warning("fireflies lookup failed for %s: %s", account_id, e)

    return days_to_renewal, days_since_call


def _stagnation(last_touch_at: Any, now: datetime) -> int | None:
    if not last_touch_at:
        return None
    if isinstance(last_touch_at, str):
        try:
            last = datetime.fromisoformat(last_touch_at.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        last = last_touch_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).days


def _already_scored_today(account_id: str, now: datetime) -> bool:
    engine = get_engine()
    start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT 1 FROM cs_churn_risk
                    WHERE account_id = :a AND created_at >= :s LIMIT 1"""
            ),
            {"a": account_id, "s": start_of_day},
        ).fetchone()
    return bool(row)


def _persist(account_id: str, result: scoring.ChurnScore, now: datetime) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_churn_risk (account_id, score, tier, factors_json, created_at)
                   VALUES (:a, :s, :t, :f, :now)"""
            ),
            {
                "a": account_id,
                "s": result.score,
                "t": result.tier,
                "f": json.dumps(
                    {
                        "factors": result.factors,
                        "contributions": result.contributions,
                    }
                ),
                "now": now,
            },
        )


def _open_task(
    account_id: str, name: str | None, result: scoring.ChurnScore, now: datetime
) -> None:
    source = f"cs:churn_risk:{account_id}:{now.date().isoformat()}"
    priority = "urgent" if result.tier >= scoring.TIER_JACKIE else "high"
    title = f"Churn risk {result.score} (tier {result.tier}) — {name or account_id}"
    top_factors = sorted(result.contributions.items(), key=lambda kv: kv[1], reverse=True)[:3]
    description = (
        f"Account {name or account_id} scored {result.score}. "
        f"Top factors: {', '.join(f'{k}={v}' for k, v in top_factors if v > 0)}."
    )
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"), {"s": source}
        ).fetchone()
        if exists:
            return
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('cs', :t, :d, 'pending', :p, 'churn_risk', :s, 'blaine', :m)"""
            ),
            {
                "t": title,
                "d": description,
                "p": priority,
                "s": source,
                "m": json.dumps({"account_id": account_id, "score": result.score, "tier": result.tier}),
            },
        )


def _emit_alert(
    account_id: str,
    name: str | None,
    result: scoring.ChurnScore,
    sender: SlackSender,
) -> None:
    mention = f" {JACKIE_SLACK_HANDLE}" if result.tier >= scoring.TIER_JACKIE else ""
    top = sorted(result.contributions.items(), key=lambda kv: kv[1], reverse=True)[:3]
    top_txt = ", ".join(f"{k}={v:.0f}" for k, v in top if v > 0) or "n/a"
    text_ = (
        f":rotating_light: *Churn risk tier {result.tier}* — {name or account_id}{mention}\n"
        f"Score: `{result.score}` | Top factors: `{top_txt}`\n"
        f"Account: `{account_id}`"
    )
    sender.send(ALERT_CHANNEL, text_)


async def run_sweep(
    *,
    sf_mcp: Any | None = None,
    fireflies_mcp: Any | None = None,
    slack_sender: SlackSender | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Score every account in cs_account_health. Returns per-tier counters."""
    now = now or datetime.now(timezone.utc)
    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf
    if fireflies_mcp is None:
        from shared.mcp import fireflies_mcp as _ff
        fireflies_mcp = _ff
    sender = slack_sender or SlackSender()

    counters = {"scored": 0, "tier_50": 0, "tier_70": 0, "tier_85": 0, "skipped_today": 0}
    for row in _iter_accounts():
        aid = row["account_id"]
        if _already_scored_today(aid, now):
            counters["skipped_today"] += 1
            continue

        inputs = scoring.ScoringInputs(
            health_current=row.get("current_health"),
            health_prev_30d_avg=_prev_30d_avg(aid, now),
            nps_category=row.get("nps_category") or "unknown",
        )
        inputs.cases_last_30d, inputs.cases_prior_30d = _case_counts(aid, sf_mcp, now)
        inputs.days_until_renewal, inputs.days_since_last_call = _renewal_gap(
            aid, sf_mcp, fireflies_mcp, now
        )
        inputs.days_since_last_activity = _stagnation(row.get("last_touch_at"), now)

        result = scoring.score_account(inputs)
        _persist(aid, result, now)
        counters["scored"] += 1

        if result.tier == 0:
            continue
        counters[f"tier_{result.tier}"] += 1

        # Tier-50 is log-only. Tier ≥70 requires ≥2 non-zero factors to alert.
        if result.tier >= scoring.TIER_BLAINE:
            if scoring.non_zero_factor_count(result.factors) < 2:
                log.info(
                    "suppressing tier-%d alert for %s: only one non-zero factor",
                    result.tier, aid,
                )
                continue
            _emit_alert(aid, row.get("name"), result, sender)
            _open_task(aid, row.get("name"), result, now)

    log.info("cs-churn-sweep complete: %s", counters)
    return counters
