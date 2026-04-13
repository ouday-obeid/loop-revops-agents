"""Vitally health poller + drop detector.

Runs every 2h (cs-health-poll). Each run:
  1. Iterate all Vitally accounts
  2. Resolve SF Account.Id via deterministic externalId
  3. Upsert cs_account_health (current) + append to cs_account_health_history
  4. Detect drops ≥10 points vs. peak of last 7 days → Slack alert to #agent-cs-log
  5. Stamp vitally_uid_resolution match rate in integration_health

Drop alert routing for M1: post to shared channel `#agent-cs-log` with a
mrkdwn summary that names the account owner. Full DM-to-CSM routing layers in
at M9 once the SF User → Slack ID lookup is built.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.slack_dispatcher import SlackSender

from agents.cs.health import uid_resolver
from agents.cs.health.vitally_client import VitallyClient, classify_nps

log = logging.getLogger(__name__)

ALERT_CHANNEL = "#agent-cs-log"
DROP_THRESHOLD = 10.0
DROP_WINDOW_DAYS = 7


def _extract_health_score(acct: dict[str, Any]) -> float | None:
    hs = acct.get("healthScore") or acct.get("health_score")
    if isinstance(hs, dict):
        val = hs.get("current") or hs.get("score")
    else:
        val = hs
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _extract_nps(acct: dict[str, Any]) -> tuple[int | None, datetime | None]:
    nps = acct.get("npsLatest") or acct.get("nps_latest") or acct.get("nps")
    if not isinstance(nps, dict):
        return None, None
    score = nps.get("score")
    at_raw = nps.get("respondedAt") or nps.get("responded_at")
    try:
        score_int = int(score) if score is not None else None
    except (TypeError, ValueError):
        score_int = None
    at = _parse_iso(at_raw) if at_raw else None
    return score_int, at


def _parse_iso(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _peak_in_window(account_id: str, since: datetime) -> float | None:
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT MAX(score) AS peak
                     FROM cs_account_health_history
                    WHERE account_id = :a AND checked_at >= :s AND score IS NOT NULL"""
            ),
            {"a": account_id, "s": since},
        ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _upsert_health(record: dict[str, Any]) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health
                     (account_id, vitally_uid, name, score, nps_score, nps_category,
                      nps_at, last_touch_at, checked_at)
                   VALUES (:aid, :vid, :name, :sc, :nps, :npsc, :npsa, :last, :now)
                   ON CONFLICT(account_id) DO UPDATE SET
                     vitally_uid = excluded.vitally_uid,
                     name = excluded.name,
                     score = excluded.score,
                     nps_score = excluded.nps_score,
                     nps_category = excluded.nps_category,
                     nps_at = excluded.nps_at,
                     last_touch_at = excluded.last_touch_at,
                     checked_at = excluded.checked_at"""
            ),
            record,
        )
        conn.execute(
            text(
                """INSERT INTO cs_account_health_history
                     (account_id, score, nps_score, checked_at)
                   VALUES (:aid, :sc, :nps, :now)"""
            ),
            {"aid": record["aid"], "sc": record["sc"], "nps": record["nps"], "now": record["now"]},
        )


def _emit_drop_alert(
    record: dict[str, Any], peak: float, sender: SlackSender | None = None
) -> None:
    drop = peak - (record["sc"] or 0)
    sender = sender or SlackSender()
    text_ = (
        f":warning: *Vitally health drop* — {record['name'] or record['aid']}\n"
        f"Current: `{record['sc']:.0f}`  |  Peak (7d): `{peak:.0f}`  |  Drop: `{drop:.0f} pts`\n"
        f"NPS: `{record['npsc']}`  |  Account: `{record['aid']}`\n"
        f"_Suggested: review last 3 Fireflies calls + open cases; contact CSM._"
    )
    sender.send(ALERT_CHANNEL, text_)


async def poll(
    client: VitallyClient | None = None,
    *,
    slack_sender: SlackSender | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Run one poll cycle. Returns counters for telemetry/tests."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=DROP_WINDOW_DAYS)
    owns_client = client is None
    client = client or VitallyClient()

    total = 0
    matched = 0
    drops = 0
    try:
        async for acct in client.iter_accounts():
            total += 1
            account_id = uid_resolver.resolve(acct)
            if not account_id:
                uid_resolver.log_miss(acct)
                continue
            matched += 1
            score = _extract_health_score(acct)
            nps_score, nps_at = _extract_nps(acct)
            nps_cat = classify_nps(nps_score)
            last_touch = _parse_iso(acct.get("lastSeenAt") or acct.get("last_seen_at") or "")
            record = {
                "aid": account_id,
                "vid": str(acct.get("id") or ""),
                "name": acct.get("name"),
                "sc": score,
                "nps": nps_score,
                "npsc": nps_cat,
                "npsa": nps_at,
                "last": last_touch,
                "now": now,
            }
            prior_peak = _peak_in_window(account_id, since)
            _upsert_health(record)
            if (
                score is not None
                and prior_peak is not None
                and (prior_peak - score) >= DROP_THRESHOLD
            ):
                drops += 1
                _emit_drop_alert(record, prior_peak, slack_sender)
    finally:
        if owns_client:
            await client.close()

    uid_resolver.record_match_rate(total=total, matched=matched)
    log.info("cs-health-poll complete: total=%d matched=%d drops=%d", total, matched, drops)
    return {"total": total, "matched": matched, "drops": drops}
