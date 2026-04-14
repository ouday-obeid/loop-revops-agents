"""Persist daily ScoredDeal rows to `pipeline_snapshots` — the append-only
history that powers mover detection, AE-cohort slicing, and the backtest
replay script.

The morning cron chain is: fetch → score → snapshot. Rerunning is idempotent
(`ON CONFLICT DO NOTHING` on `(snapshot_date, opp_id)`), so a retried cron or a
manually triggered `@oo slt snapshot` never double-writes. We log a warning
when a rerun produced zero inserts — useful signal if the cron accidentally
fires twice before anything changed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date
from typing import Any, Iterable, Mapping

from sqlalchemy import text

from shared.db.connection import get_engine

from agents.slt_metrics.types import OppRecord, ScoredDeal

log = logging.getLogger(__name__)


_INSERT_SQL = text(
    """
    INSERT INTO pipeline_snapshots
        (snapshot_date, opp_id, stage, amount, acv, close_date,
         owner_id, owner_name, account_id, segment,
         score, category, probability, weighted_acv, metadata)
    VALUES
        (:snapshot_date, :opp_id, :stage, :amount, :acv, :close_date,
         :owner_id, :owner_name, :account_id, :segment,
         :score, :category, :probability, :weighted_acv, :metadata)
    ON CONFLICT (snapshot_date, opp_id) DO NOTHING
    """
)


def write_snapshot(
    deals: Iterable[ScoredDeal],
    *,
    snapshot_date: date,
) -> int:
    """Insert one `pipeline_snapshots` row per ScoredDeal. Returns rows inserted.

    Zero is a valid return — it means the snapshot for this date was already
    written. Caller decides whether to warn.
    """
    rows = [_deal_to_row(deal, snapshot_date) for deal in deals]
    if not rows:
        log.info("write_snapshot: no deals provided for %s", snapshot_date)
        return 0

    engine = get_engine()
    inserted = 0
    with engine.begin() as conn:
        for row in rows:
            result = conn.execute(_INSERT_SQL, row)
            inserted += result.rowcount or 0

    if inserted == 0:
        log.warning(
            "write_snapshot: snapshot_date=%s already has rows for all %d deals; no inserts",
            snapshot_date, len(rows),
        )
    else:
        log.info("write_snapshot: inserted %d/%d rows for %s",
                 inserted, len(rows), snapshot_date)
    return inserted


def write_unscored_snapshot(
    opps: Iterable[OppRecord],
    *,
    snapshot_date: date,
) -> int:
    """Pre-scoring snapshot — score columns are NULL.

    Bridge API so the 06:30 cron can run before D6 wires the scoring pillars.
    Once `forecast.scorer.score_all` lands, the morning job switches to
    `write_snapshot(score_all(opps), ...)` and this helper is retained only
    for diagnostic / backfill scripts.
    """
    rows = [_opp_to_unscored_row(o, snapshot_date) for o in opps]
    if not rows:
        log.info("write_unscored_snapshot: no opps provided for %s", snapshot_date)
        return 0

    engine = get_engine()
    inserted = 0
    with engine.begin() as conn:
        for row in rows:
            result = conn.execute(_INSERT_SQL, row)
            inserted += result.rowcount or 0
    log.info("write_unscored_snapshot: inserted %d/%d rows for %s",
             inserted, len(rows), snapshot_date)
    return inserted


def read_snapshot(snapshot_date: date) -> list[dict[str, Any]]:
    """Load all rows for a given snapshot date, metadata deserialized.

    Returned rows are plain dicts — the mover detector and the scorecards
    consume them directly without dataclass rehydration.
    """
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                SELECT snapshot_date, opp_id, stage, amount, acv, close_date,
                       owner_id, owner_name, account_id, segment,
                       score, category, probability, weighted_acv, metadata,
                       created_at
                  FROM pipeline_snapshots
                 WHERE snapshot_date = :d
                 ORDER BY acv DESC NULLS LAST, opp_id
                """
            ),
            {"d": snapshot_date},
        )
        rows = [dict(r) for r in result.mappings().all()]

    for row in rows:
        if row.get("metadata"):
            try:
                row["metadata"] = json.loads(row["metadata"])
            except json.JSONDecodeError:
                log.warning(
                    "read_snapshot: corrupt metadata JSON for opp_id=%s snapshot=%s",
                    row.get("opp_id"), snapshot_date,
                )
                row["metadata"] = {}
        else:
            row["metadata"] = {}
    return rows


def latest_snapshot_date(before: date | None = None) -> date | None:
    """Most recent snapshot date strictly earlier than `before` (or overall).

    Used by the mover detector to find the previous snapshot for a diff; pass
    today as `before` to get yesterday's effective snapshot regardless of
    weekends or skipped cron runs.
    """
    engine = get_engine()
    with engine.begin() as conn:
        if before is None:
            query = "SELECT MAX(snapshot_date) FROM pipeline_snapshots"
            params: Mapping[str, Any] = {}
        else:
            query = "SELECT MAX(snapshot_date) FROM pipeline_snapshots WHERE snapshot_date < :d"
            params = {"d": before}
        result = conn.execute(text(query), params).scalar()
    if result is None:
        return None
    if isinstance(result, date):
        return result
    # SQLite returns ISO strings when no row-level adapter is registered.
    try:
        return date.fromisoformat(str(result)[:10])
    except ValueError:
        return None


# ------------------------------------------------------------------ helpers

def _deal_to_row(deal: ScoredDeal, snapshot_date: date) -> dict[str, Any]:
    return {
        "snapshot_date": snapshot_date,
        "opp_id": deal.opp_id,
        "stage": deal.stage,
        "amount": deal.amount,
        "acv": deal.acv,
        "close_date": deal.close_date,
        "owner_id": _owner_id_from_raw(deal.raw),
        "owner_name": deal.owner_name,
        "account_id": _account_id_from_raw(deal.raw),
        "segment": deal.segment,
        "score": deal.score,
        "category": deal.category,
        "probability": deal.probability,
        "weighted_acv": deal.weighted_acv,
        "metadata": json.dumps(_serialize_metadata(deal), default=_json_default),
    }


def _opp_to_unscored_row(opp: OppRecord, snapshot_date: date) -> dict[str, Any]:
    return {
        "snapshot_date": snapshot_date,
        "opp_id": opp.id,
        "stage": opp.stage,
        "amount": opp.amount,
        "acv": opp.acv,
        "close_date": opp.close_date,
        "owner_id": opp.owner_id,
        "owner_name": opp.owner_name,
        "account_id": opp.account_id,
        "segment": opp.segment,
        "score": None,
        "category": None,
        "probability": None,
        "weighted_acv": None,
        "metadata": json.dumps(
            {"weights_version": None, "sf_raw": opp.raw},
            default=_json_default,
        ),
    }


def _owner_id_from_raw(raw: OppRecord | None) -> str | None:
    return raw.owner_id if raw else None


def _account_id_from_raw(raw: OppRecord | None) -> str | None:
    return raw.account_id if raw else None


def _serialize_metadata(deal: ScoredDeal) -> dict[str, Any]:
    """Flat metadata blob: pillar breakdown, risk flags, raw SF row.

    Keeping the raw SF row (as-fetched) makes the backtest replay script
    reproducible — it's the same input the scorer saw on that day.
    """
    meta: dict[str, Any] = {
        "pillars": {k: asdict(v) for k, v in deal.pillars.items()},
        "risk_flags": list(deal.risk_flags),
        "weights_version": deal.weights_version,
    }
    if deal.raw is not None:
        meta["sf_raw"] = deal.raw.raw
    return meta


def _json_default(obj: Any) -> Any:
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"unserializable: {type(obj).__name__}")
