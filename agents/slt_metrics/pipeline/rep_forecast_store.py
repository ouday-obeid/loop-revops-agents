"""Storage for rep-submitted forecasts (`rep_forecasts` table).

Written by the `@oo slt ingest-rep-forecast` dispatcher command. Read by
`jobs._build_payload` at briefing time and surfaced on the Rep Forecast
sheet's "Rep Submitted Forecast" column.

One row per (owner_name, quarter). Re-ingesting a quarter upserts; the
most-recent `submitted_at` wins.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import text

from shared.db.connection import get_engine

from agents.slt_metrics.types import RepForecastEntry

log = logging.getLogger(__name__)


_UPSERT_SQL = text(
    """
    INSERT INTO rep_forecasts
        (owner_name, quarter, commit_acv, best_case_acv, notes, source, submitted_at)
    VALUES
        (:owner_name, :quarter, :commit_acv, :best_case_acv, :notes, :source, :submitted_at)
    ON CONFLICT (owner_name, quarter) DO UPDATE SET
        commit_acv    = excluded.commit_acv,
        best_case_acv = excluded.best_case_acv,
        notes         = excluded.notes,
        source        = excluded.source,
        submitted_at  = excluded.submitted_at
    """
)


_READ_QUARTER_SQL = text(
    """
    SELECT owner_name, quarter, commit_acv, best_case_acv, notes, source, submitted_at
    FROM rep_forecasts
    WHERE quarter = :quarter
    """
)


def upsert_rep_forecasts(
    entries: Iterable[RepForecastEntry],
    *,
    source: str | None = None,
) -> int:
    """Bulk-upsert rep forecasts. `source` overrides per-entry source when set
    (typical case: the ingest command passes the file path once)."""
    rows = []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for e in entries:
        rows.append({
            "owner_name": e.owner_name,
            "quarter": e.quarter,
            "commit_acv": e.commit_acv,
            "best_case_acv": e.best_case_acv,
            "notes": e.notes,
            "source": source if source is not None else e.source,
            "submitted_at": e.submitted_at or now,
        })
    if not rows:
        return 0
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(_UPSERT_SQL, rows)
    log.info("upsert_rep_forecasts: wrote %d rows (source=%s)", len(rows), source)
    return len(rows)


def read_rep_forecasts(quarter: str) -> dict[str, RepForecastEntry]:
    """Return a dict keyed by owner_name for one quarter's submissions."""
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(_READ_QUARTER_SQL, {"quarter": quarter})
        rows = result.mappings().all()
    out: dict[str, RepForecastEntry] = {}
    for row in rows:
        submitted = row["submitted_at"]
        if isinstance(submitted, str):
            try:
                submitted = datetime.fromisoformat(submitted)
            except ValueError:
                submitted = None
        out[row["owner_name"]] = RepForecastEntry(
            owner_name=row["owner_name"],
            quarter=row["quarter"],
            commit_acv=row["commit_acv"],
            best_case_acv=row["best_case_acv"],
            notes=row["notes"],
            source=row["source"],
            submitted_at=submitted,
        )
    return out
