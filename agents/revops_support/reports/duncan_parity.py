"""Duncan parity report — agent-handled vs Duncan-billed SF admin tasks.

Phase-out instrumentation. Measured weekly; never drives an automated
retainer-reduction decision (per plan, that's a Phase 3 session).

Aggregates over a 7-day window:
- agent_handled: audit_log rows by revops_support (grouped by action family).
- duncan_billed: tasks rows with source starting 'duncan:' (manual input by O
  at the end of each week, summarising what Duncan's retainer touched).

Output: CSV at `${REVOPS_REPO_ROOT}/var/reports/duncan_parity_<YYYY-MM-DD>.csv`
and a console summary.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)

DUNCAN_SOURCE_PREFIX = "duncan:"


@dataclass(frozen=True)
class ParityRow:
    category: str
    agent_handled: int
    duncan_billed: int

    @property
    def delta(self) -> int:
        return self.agent_handled - self.duncan_billed


def _reports_dir() -> Path:
    root = get_config("REVOPS_REPO_ROOT") or str(Path(__file__).resolve().parents[3])
    d = Path(root) / "var" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _normalize_action(action: str) -> str:
    """Map audit_log.action → a comparable category name.

    Normalizing keeps the CSV stable even as we add new action types; e.g.
    both `sf_update` and `sf_bulk_update` roll up to `sf_update`.
    """
    if not action:
        return "other"
    if action.startswith("sf_bulk_"):
        return "sf_bulk"
    if action.startswith("sf_create"):
        return "sf_create"
    if action.startswith("sf_update"):
        return "sf_update"
    if action.startswith("sf_delete"):
        return "sf_delete"
    if action.startswith("sf_schema"):
        return "sf_schema"
    if action.startswith("sf_query"):
        return "sf_query"
    return action


def collect(week_of: date | None = None, *, window_days: int = 7) -> list[ParityRow]:
    """Build one ParityRow per category for the week ending `week_of`."""
    end = datetime.combine(week_of or date.today(), datetime.min.time())
    start = end - timedelta(days=window_days)

    engine = get_engine()
    with engine.begin() as conn:
        agent_rows = conn.execute(
            text(
                "SELECT action FROM audit_log "
                "WHERE agent_name = 'revops_support' "
                "AND timestamp >= :start AND timestamp < :end"
            ),
            {"start": start, "end": end},
        ).fetchall()
        duncan_rows = conn.execute(
            text(
                "SELECT category FROM tasks "
                "WHERE source LIKE :prefix "
                "AND created_at >= :start AND created_at < :end"
            ),
            {"prefix": f"{DUNCAN_SOURCE_PREFIX}%", "start": start, "end": end},
        ).fetchall()

    agent_counts: dict[str, int] = {}
    for (action,) in agent_rows:
        key = _normalize_action(action or "")
        agent_counts[key] = agent_counts.get(key, 0) + 1

    duncan_counts: dict[str, int] = {}
    for (category,) in duncan_rows:
        key = (category or "other").strip() or "other"
        duncan_counts[key] = duncan_counts.get(key, 0) + 1

    keys = sorted(set(agent_counts) | set(duncan_counts))
    return [
        ParityRow(
            category=k,
            agent_handled=agent_counts.get(k, 0),
            duncan_billed=duncan_counts.get(k, 0),
        )
        for k in keys
    ]


def write_csv(rows: list[ParityRow], week_of: date | None = None) -> Path:
    week_of = week_of or date.today()
    path = _reports_dir() / f"duncan_parity_{week_of.isoformat()}.csv"
    with path.open("w", newline="", encoding="utf-8") as fp:
        w = csv.writer(fp)
        w.writerow(["category", "agent_handled", "duncan_billed", "delta"])
        for r in rows:
            w.writerow([r.category, r.agent_handled, r.duncan_billed, r.delta])
        total = ParityRow(
            category="TOTAL",
            agent_handled=sum(r.agent_handled for r in rows),
            duncan_billed=sum(r.duncan_billed for r in rows),
        )
        w.writerow([total.category, total.agent_handled, total.duncan_billed, total.delta])
    log.info("wrote parity csv → %s (%d rows)", path, len(rows))
    return path


def report(week_of: date | None = None) -> tuple[Path, list[ParityRow]]:
    rows = collect(week_of=week_of)
    path = write_csv(rows, week_of=week_of)
    return path, rows


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Weekly Duncan parity report")
    ap.add_argument("--week", help="YYYY-MM-DD ending the 7-day window (default: today)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    wk = date.fromisoformat(args.week) if args.week else None
    path, rows = report(week_of=wk)
    print(f"parity report: {path}")
    for r in rows:
        print(f"  {r.category}: agent={r.agent_handled} duncan={r.duncan_billed} delta={r.delta:+d}")
