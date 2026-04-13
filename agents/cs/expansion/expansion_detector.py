"""Expansion signal detector — daily sweep for upsell-worthy account motion.

Three signal sources:
  1. Fireflies keywords in last 24h calls
  2. Location__c net adds in last 30d per Account
  3. Brand_Logo__c net adds in last 30d per Account

Emits one `cs` task per (account, signal_type) per day, category=`expansion`.
No Slack alerts — surfaced in the weekly Jackie digest (M8) to avoid fatigue.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)

KEYWORDS = [
    "expand", "expansion",
    "add location", "adding location", "new location",
    "more seats", "add seats",
    "new brand", "add brand",
    "upgrade",
    "upsell",
]
FIREFLIES_LOOKBACK_HOURS = 24
LOCATION_LOOKBACK_DAYS = 30
LOCATION_NET_ADDS_THRESHOLD = 2
BRAND_NET_ADDS_THRESHOLD = 1


def _has_keyword(text_: str) -> str | None:
    if not text_:
        return None
    low = text_.lower()
    for kw in KEYWORDS:
        if kw in low:
            return kw
    return None


def _fireflies_signals(fireflies_mcp: Any, now: datetime) -> list[dict[str, Any]]:
    since = now - timedelta(hours=FIREFLIES_LOOKBACK_HOURS)
    try:
        transcripts = fireflies_mcp.list_transcripts(from_date=since.isoformat()) or []
    except Exception as e:
        log.warning("expansion fireflies lookup failed: %s", e)
        return []
    signals = []
    for t in transcripts:
        text_ = " ".join(
            str(t.get(k) or "") for k in ("summary", "title", "transcript", "notes")
        )
        kw = _has_keyword(text_)
        if not kw:
            continue
        acct = t.get("account_id") or t.get("sf_account_id")
        if not acct:
            continue
        signals.append(
            {
                "account_id": acct,
                "signal": "call_keyword",
                "keyword": kw,
                "transcript_id": t.get("id"),
                "title": t.get("title") or "(untitled)",
            }
        )
    return signals


def _location_signals(sf_mcp: Any, now: datetime) -> list[dict[str, Any]]:
    cutoff = (now - timedelta(days=LOCATION_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        q = (
            "SELECT Account__c, COUNT(Id) c FROM Location__c "
            f"WHERE CreatedDate > {cutoff} "
            "GROUP BY Account__c"
        )
        r = sf_mcp.soql_query(q, limit=500)
    except Exception as e:
        log.warning("expansion location lookup failed: %s", e)
        return []
    out = []
    for row in r.get("records") or []:
        acct = row.get("Account__c")
        count = int(row.get("c", 0) or 0)
        if acct and count >= LOCATION_NET_ADDS_THRESHOLD:
            out.append({"account_id": acct, "signal": "location_growth", "count": count})
    return out


def _brand_signals(sf_mcp: Any, now: datetime) -> list[dict[str, Any]]:
    cutoff = (now - timedelta(days=LOCATION_LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        q = (
            "SELECT Account__c, COUNT(Id) c FROM Brand_Logo__c "
            f"WHERE CreatedDate > {cutoff} "
            "GROUP BY Account__c"
        )
        r = sf_mcp.soql_query(q, limit=500)
    except Exception as e:
        log.warning("expansion brand lookup failed: %s", e)
        return []
    out = []
    for row in r.get("records") or []:
        acct = row.get("Account__c")
        count = int(row.get("c", 0) or 0)
        if acct and count >= BRAND_NET_ADDS_THRESHOLD:
            out.append({"account_id": acct, "signal": "brand_added", "count": count})
    return out


def _open_task(signal: dict[str, Any], now: datetime) -> bool:
    """Create expansion task (idempotent by account+signal+date). Returns True if new."""
    acct = signal["account_id"]
    kind = signal["signal"]
    source = f"cs:expansion:{acct}:{kind}:{now.date().isoformat()}"
    if kind == "call_keyword":
        title = f"Expansion signal: `{signal['keyword']}` mentioned on call — {acct}"
    elif kind == "location_growth":
        title = f"Expansion signal: {signal['count']} new locations in 30d — {acct}"
    else:
        title = f"Expansion signal: {signal['count']} new brand(s) in 30d — {acct}"
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s LIMIT 1"), {"s": source}
        ).fetchone()
        if exists:
            return False
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('cs', :t, :d, 'pending', 'medium', 'expansion', :s, 'blaine', :m)"""
            ),
            {
                "t": title,
                "d": "Review for upsell conversation. Details in metadata.",
                "s": source,
                "m": json.dumps(signal),
            },
        )
    return True


async def run_sweep(
    *,
    sf_mcp: Any | None = None,
    fireflies_mcp: Any | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf
    if fireflies_mcp is None:
        from shared.mcp import fireflies_mcp as _ff
        fireflies_mcp = _ff

    counters = {"call_keyword": 0, "location_growth": 0, "brand_added": 0, "deduped": 0}
    all_signals: list[dict[str, Any]] = []
    all_signals.extend(_fireflies_signals(fireflies_mcp, now))
    all_signals.extend(_location_signals(sf_mcp, now))
    all_signals.extend(_brand_signals(sf_mcp, now))

    for sig in all_signals:
        created = _open_task(sig, now)
        if created:
            counters[sig["signal"]] += 1
        else:
            counters["deduped"] += 1
    log.info("cs-expansion-scan complete: %s", counters)
    return counters
