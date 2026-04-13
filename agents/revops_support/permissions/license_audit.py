"""Identify Salesforce licenses that look reclaimable.

Criterion (Phase 1, conservative):
  - `User.IsActive = true`
  - `User.LastLoginDate = null` OR `LastLoginDate < now - INACTIVE_DAYS`
  - Excludes profile names matching the allow-list (integration users, API
    service accounts that legitimately never log in via UI).

Output: a list of `InactiveUser` rows with estimated monthly savings. A task
is opened per user so O can decide whether to deactivate.

License recovery is NOT executed here — that's the `offboarding` module's
responsibility. This is pure detection + suggestion.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)

INACTIVE_DAYS = 60
ESTIMATED_MONTHLY_COST_USD = 70
INTEGRATION_PROFILE_PREFIXES: tuple[str, ...] = (
    "Integration", "API", "Service", "Platform",
)
# Substrings tolerated anywhere in the profile name — catches SF's packaged
# "Sales Insights Integration User" / "Analytics Cloud Integration User"
# shapes where the vendor prefix comes before "Integration User".
INTEGRATION_PROFILE_SUBSTRINGS: tuple[str, ...] = (
    "Integration User",
)
TASK_CATEGORY = "sf_license_audit"


@dataclass
class InactiveUser:
    id: str
    username: str
    email: str | None
    last_login: str | None
    profile_name: str | None
    monthly_cost_usd: int = ESTIMATED_MONTHLY_COST_USD


def _cutoff_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _is_integration_profile(profile_name: str | None) -> bool:
    if not profile_name:
        return False
    if any(profile_name.startswith(p) for p in INTEGRATION_PROFILE_PREFIXES):
        return True
    return any(s in profile_name for s in INTEGRATION_PROFILE_SUBSTRINGS)


def _query_candidates(
    *,
    soql_query=None,
    inactive_days: int = INACTIVE_DAYS,
) -> list[InactiveUser]:
    sq = soql_query or salesforce_mcp.soql_query
    cutoff = _cutoff_iso(inactive_days)
    q = (
        "SELECT Id, Username, Email, LastLoginDate, Profile.Name "
        "FROM User "
        "WHERE IsActive = true "
        f"AND (LastLoginDate = null OR LastLoginDate < {cutoff})"
    )
    r = sq(q, limit=500)
    out: list[InactiveUser] = []
    for rec in r.get("records") or []:
        profile = rec.get("Profile") or {}
        profile_name = profile.get("Name") if isinstance(profile, dict) else None
        if _is_integration_profile(profile_name):
            continue
        out.append(
            InactiveUser(
                id=rec.get("Id") or "",
                username=rec.get("Username") or "",
                email=rec.get("Email"),
                last_login=rec.get("LastLoginDate"),
                profile_name=profile_name,
            )
        )
    return out


def _surface(user: InactiveUser) -> None:
    source = f"revops_support:license_audit:{user.id}"
    engine = get_engine()
    with engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tasks WHERE source = :s AND status != 'completed' LIMIT 1"),
            {"s": source},
        ).fetchone()
        if exists:
            return
        conn.execute(
            text(
                """INSERT INTO tasks (agent_name, title, description, status, priority,
                                      category, source, assignee, metadata)
                   VALUES ('revops_support', :t, :d, 'pending', 'low',
                           :c, :s, 'O', :m)"""
            ),
            {
                "t": (
                    f"Reclaim license: {user.username} inactive since "
                    f"{user.last_login or 'never logged in'}"
                ),
                "d": (
                    f"User {user.username} ({user.email or 'no email'}) has not "
                    f"logged in for >= {INACTIVE_DAYS} days. Estimated savings: "
                    f"${ESTIMATED_MONTHLY_COST_USD}/mo. Profile: "
                    f"{user.profile_name or 'n/a'}. Decide whether to offboard."
                ),
                "c": TASK_CATEGORY,
                "s": source,
                "m": json.dumps({
                    "user_id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "last_login": user.last_login,
                    "profile_name": user.profile_name,
                    "monthly_cost_usd": ESTIMATED_MONTHLY_COST_USD,
                }),
            },
        )


def run(*, soql_query=None, inactive_days: int = INACTIVE_DAYS) -> list[InactiveUser]:
    candidates = _query_candidates(
        soql_query=soql_query, inactive_days=inactive_days,
    )
    for u in candidates:
        _surface(u)
    total_monthly = sum(u.monthly_cost_usd for u in candidates)
    log.info(
        "license_audit: %d reclaimable user(s); est. recovery $%d/mo",
        len(candidates), total_monthly,
    )
    return candidates
