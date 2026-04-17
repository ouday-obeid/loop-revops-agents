"""Seed the `rep_config` table from agents.slt_metrics.pipeline.planning.

Idempotent — uses SQLite `INSERT OR REPLACE` keyed on `owner_name`. Safe to
re-run whenever the roster in planning.py changes. Rows marked active=1;
flip to 0 by hand when a rep departs (we intentionally do NOT delete — it
preserves historical `pipeline_snapshots` joins).

Usage:
    cd $REVOPS_REPO_ROOT && source .venv/bin/activate
    python scripts/seed_slt_rep_config.py
"""
from __future__ import annotations

import json
import sys

from sqlalchemy import text

from agents.slt_metrics.pipeline.planning import (
    AE_ROSTER,
    MANAGER_GROUPS,
    SDR_ROSTER,
    RosterEntry,
)
from shared.db.connection import get_engine


DEFAULT_ATTAINMENT_FLOOR_PCT = 0.70


def _manager_for(name: str) -> str | None:
    for mgr, members in MANAGER_GROUPS.items():
        if name in members:
            return mgr
    return None


def _row(entry: RosterEntry) -> dict:
    annual = entry.annual_quota or None
    quarterly = (annual / 4.0) if annual else None
    metadata = {
        "status": entry.status,
        "manager": _manager_for(entry.name),
        "source": "planning.py",
    }
    return {
        "owner_name": entry.name,
        "role": entry.role,
        "team": entry.segment,
        "quarterly_quota": quarterly,
        "annual_quota": annual,
        "attainment_floor_pct": DEFAULT_ATTAINMENT_FLOOR_PCT,
        "active": 1,
        "metadata": json.dumps(metadata),
    }


_UPSERT = text("""
    INSERT OR REPLACE INTO rep_config
        (owner_name, role, team, quarterly_quota, annual_quota,
         attainment_floor_pct, active, metadata, updated_at)
    VALUES
        (:owner_name, :role, :team, :quarterly_quota, :annual_quota,
         :attainment_floor_pct, :active, :metadata, CURRENT_TIMESTAMP)
""")


def main() -> int:
    roster = list(AE_ROSTER) + list(SDR_ROSTER)
    if not roster:
        print("planning.AE_ROSTER + SDR_ROSTER are empty — nothing to seed", file=sys.stderr)
        return 1

    engine = get_engine()
    with engine.begin() as conn:
        for entry in roster:
            conn.execute(_UPSERT, _row(entry))

    # Summary
    with engine.connect() as conn:
        by_role = dict(
            conn.execute(text(
                "SELECT role, COUNT(*) FROM rep_config WHERE active=1 GROUP BY role"
            )).fetchall()
        )
    print(f"seeded {len(roster)} rep_config rows")
    for role, n in sorted(by_role.items(), key=lambda kv: (kv[0] or "")):
        print(f"  {role or '(null)':<14} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
