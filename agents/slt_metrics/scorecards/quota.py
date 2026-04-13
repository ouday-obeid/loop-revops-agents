"""Per-AE / team quota pull + pacing vs straight-line.

Canonical source is the `rep_config` table (migration 0005). The table is
empty at deploy — seeding happens via either `@oo slt quota set <rep>=<amt>`
or a one-shot CSV import by O. Until then, every owner returns `None` and
`build_quota_report` treats them as "no quota on file".

Public API:
  - `load_rep_quotas()` → {owner_name: quarterly_quota}
  - `set_rep_quota(...)` — upsert one rep's quota row
  - `quarter_elapsed_pct(today, quarter_start, quarter_end)` — straight-line
    pacing fraction used by `forecast.gap_to_quota.build_quota_report`.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine

log = logging.getLogger(__name__)

DEFAULT_ATTAINMENT_FLOOR_PCT: float = 0.70


@dataclass(frozen=True)
class RepConfig:
    owner_name: str
    role: str | None
    team: str | None
    quarterly_quota: float | None
    annual_quota: float | None
    attainment_floor_pct: float
    active: bool


def load_rep_quotas(*, role: str | None = "AE") -> dict[str, float]:
    """Return {owner_name: quarterly_quota} for active reps.

    Defaults to AE quotas (the forecast gap report is AE-scoped); pass
    `role=None` to pull every row including SDRs + managers.
    """
    engine = get_engine()
    with engine.begin() as conn:
        if role is None:
            rows = conn.execute(
                text(
                    "SELECT owner_name, quarterly_quota FROM rep_config "
                    "WHERE active = 1 AND quarterly_quota IS NOT NULL"
                )
            ).fetchall()
        else:
            rows = conn.execute(
                text(
                    "SELECT owner_name, quarterly_quota FROM rep_config "
                    "WHERE active = 1 AND role = :role AND quarterly_quota IS NOT NULL"
                ),
                {"role": role},
            ).fetchall()
    return {row[0]: float(row[1]) for row in rows}


def load_all_rep_configs(*, active_only: bool = True) -> list[RepConfig]:
    """Full rep roster — AE scorecard + rep_risk computation use this."""
    engine = get_engine()
    with engine.begin() as conn:
        sql = (
            "SELECT owner_name, role, team, quarterly_quota, annual_quota, "
            "attainment_floor_pct, active FROM rep_config"
        )
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY team NULLS LAST, owner_name"
        rows = conn.execute(text(sql)).mappings().all()
    return [
        RepConfig(
            owner_name=r["owner_name"],
            role=r["role"],
            team=r["team"],
            quarterly_quota=(float(r["quarterly_quota"]) if r["quarterly_quota"] is not None else None),
            annual_quota=(float(r["annual_quota"]) if r["annual_quota"] is not None else None),
            attainment_floor_pct=(
                float(r["attainment_floor_pct"])
                if r["attainment_floor_pct"] is not None
                else DEFAULT_ATTAINMENT_FLOOR_PCT
            ),
            active=bool(r["active"]),
        )
        for r in rows
    ]


def set_rep_quota(
    owner_name: str,
    *,
    quarterly_quota: float | None = None,
    annual_quota: float | None = None,
    role: str | None = None,
    team: str | None = None,
    attainment_floor_pct: float | None = None,
    active: bool = True,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Upsert a rep_config row. Any None field leaves the existing value alone."""
    engine = get_engine()
    meta_json = json.dumps(metadata) if metadata is not None else None
    with engine.begin() as conn:
        existing = conn.execute(
            text("SELECT owner_name FROM rep_config WHERE owner_name = :n"),
            {"n": owner_name},
        ).scalar()
        if existing is None:
            conn.execute(
                text(
                    """
                    INSERT INTO rep_config (
                        owner_name, role, team, quarterly_quota, annual_quota,
                        attainment_floor_pct, active, metadata
                    ) VALUES (
                        :owner_name, :role, :team, :quarterly_quota, :annual_quota,
                        COALESCE(:attainment_floor_pct, :default_floor),
                        :active, :metadata
                    )
                    """
                ),
                {
                    "owner_name": owner_name,
                    "role": role, "team": team,
                    "quarterly_quota": quarterly_quota,
                    "annual_quota": annual_quota,
                    "attainment_floor_pct": attainment_floor_pct,
                    "default_floor": DEFAULT_ATTAINMENT_FLOOR_PCT,
                    "active": 1 if active else 0,
                    "metadata": meta_json,
                },
            )
            return

        # Build a COALESCE-style update so callers can change one field at a
        # time without wiping the rest.
        conn.execute(
            text(
                """
                UPDATE rep_config SET
                    role = COALESCE(:role, role),
                    team = COALESCE(:team, team),
                    quarterly_quota = COALESCE(:quarterly_quota, quarterly_quota),
                    annual_quota = COALESCE(:annual_quota, annual_quota),
                    attainment_floor_pct = COALESCE(:attainment_floor_pct, attainment_floor_pct),
                    active = :active,
                    metadata = COALESCE(:metadata, metadata),
                    updated_at = CURRENT_TIMESTAMP
                WHERE owner_name = :owner_name
                """
            ),
            {
                "owner_name": owner_name,
                "role": role, "team": team,
                "quarterly_quota": quarterly_quota,
                "annual_quota": annual_quota,
                "attainment_floor_pct": attainment_floor_pct,
                "active": 1 if active else 0,
                "metadata": meta_json,
            },
        )


# ------------------------------------------------------------------ quarter math

def quarter_bounds(today: date) -> tuple[date, date]:
    """Return (quarter_start, quarter_end) containing `today`, fiscal = calendar."""
    # Q1 Jan-Mar, Q2 Apr-Jun, Q3 Jul-Sep, Q4 Oct-Dec.
    month_starts = ((1, 1), (4, 1), (7, 1), (10, 1))
    year = today.year
    starts = [date(year, m, d) for (m, d) in month_starts]
    ends = [
        date(year, 3, 31),
        date(year, 6, 30),
        date(year, 9, 30),
        date(year, 12, 31),
    ]
    for s, e in zip(starts, ends):
        if s <= today <= e:
            return s, e
    # Fallback (leap-day edge case): assume Q1 next year, shouldn't be reachable.
    return date(year + 1, 1, 1), date(year + 1, 3, 31)


def quarter_elapsed_pct(
    today: date,
    *,
    quarter_start: date | None = None,
    quarter_end: date | None = None,
) -> float:
    """Straight-line pacing fraction: 0.0 at quarter_start, 1.0 at quarter_end."""
    if quarter_start is None or quarter_end is None:
        quarter_start, quarter_end = quarter_bounds(today)
    total = (quarter_end - quarter_start).days + 1
    elapsed = (today - quarter_start).days + 1
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, elapsed / total))


def current_quarter_label(today: date) -> str:
    """Return "FY{YYYY}-Q{N}" for the quarter containing `today`."""
    q = (today.month - 1) // 3 + 1
    return f"FY{today.year}-Q{q}"
