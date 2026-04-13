"""Suppression — 5-layer read-only check before any prospecting touch.

Order (first hit wins; reason recorded):
  1. Local cache (suppression_cache, 7-day TTL)
  2. SF DoNotCall / HasOptedOutOfEmail (Lead ∪ Contact by email)
  3. Account.Type = 'Customer'  (don't re-prospect customers)
  4. Account.LastActivityDate ≤ 90 days  (already being engaged)
  5. Competitor domain list (shared/config/suppression_extras.yaml)

Fail-open policy (SF outage): return suppressed=False with source='fail_open' and
write an audit warning. Rationale — the better backstop for "should we contact
this person" is the enrollment-layer duplicate check. Freezing the pipeline on
an Apollo outage would produce 0-lead briefings. See RUNBOOK.md.

Public surface:
  is_suppressed(email, domain=None, sf_query=..., competitor_domains=...)
  add_manual(email, reason)
  clear_expired()
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text
import yaml

from agents.top_of_funnel.state import get_state_engine
from shared.governance import write_audit

log = logging.getLogger(__name__)

SF_QUERY_CALLABLE = Callable[[str], dict[str, Any]]

_AGENT_DIR = Path(__file__).parent
_REPO_ROOT = _AGENT_DIR.parent.parent
_SHARED_CONFIG = _REPO_ROOT / "shared" / "config" / "suppression_extras.yaml"
_AGENT_CONFIG_FALLBACK = _AGENT_DIR / "config" / "suppression_local.yaml"

_DEFAULT_CACHE_TTL_DAYS = 7
_RECENT_ACTIVITY_DAYS = 90

_EMAIL_RE = re.compile(r"^[^@]+@([^@]+)$")


@dataclass(frozen=True)
class SuppressionResult:
    suppressed: bool
    reason: str = ""
    source: str = ""
    # source ∈ {local_cache, manual, sf_dnc, sf_customer, sf_recent_activity,
    #           competitor, fail_open, input, none}

    def to_dict(self) -> dict[str, Any]:
        return {
            "suppressed": self.suppressed,
            "reason": self.reason,
            "source": self.source,
        }


# --------------------------------------------------------------- competitor list


def _load_competitor_domains() -> set[str]:
    """Load competitor domain set. Prefers shared/config/ (Phase 0 amendment);
    falls back to agent-local config; falls back to empty set + warn."""
    for path in (_SHARED_CONFIG, _AGENT_CONFIG_FALLBACK):
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}
            competitors = raw.get("competitors") or []
            return {row.get("domain", "").lower() for row in competitors if row.get("domain")}
    log.warning(
        "suppression: no competitor config found (checked %s, %s)",
        _SHARED_CONFIG,
        _AGENT_CONFIG_FALLBACK,
    )
    return set()


# ------------------------------------------------------------------------- cache


def _cache_lookup(email: str, ttl_days: int) -> SuppressionResult | None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT suppressed, reason, source FROM suppression_cache
                   WHERE email = :e AND checked_at >= :cutoff"""
            ),
            {"e": email.lower(), "cutoff": cutoff},
        ).fetchone()
    if row is None:
        return None
    return SuppressionResult(
        suppressed=bool(row[0]),
        reason=row[1] or "",
        source=row[2] or "local_cache",
    )


def _cache_write(email: str, result: SuppressionResult) -> None:
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO suppression_cache (email, suppressed, reason, source, checked_at)
                   VALUES (:e, :s, :r, :src, :now)
                   ON CONFLICT(email) DO UPDATE SET
                     suppressed=excluded.suppressed,
                     reason=excluded.reason,
                     source=excluded.source,
                     checked_at=excluded.checked_at"""
            ),
            {
                "e": email.lower(),
                "s": 1 if result.suppressed else 0,
                "r": result.reason,
                "src": result.source,
                "now": datetime.now(timezone.utc),
            },
        )


def clear_expired(ttl_days: int = _DEFAULT_CACHE_TTL_DAYS) -> int:
    """Remove cache rows older than ttl_days. Returns rows deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    engine = get_state_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM suppression_cache WHERE checked_at < :cutoff"),
            {"cutoff": cutoff},
        )
        return result.rowcount or 0


# ------------------------------------------------------------------------ SF check


def _soql_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _check_salesforce(email: str, sf_query: SF_QUERY_CALLABLE) -> SuppressionResult | None:
    """Returns the first SF-layer hit, or None if no layer 2–4 matched.

    Queries Lead + Contact separately (SOQL has no UNION). Fail-open on SalesforceError.
    """
    from shared.mcp.salesforce_mcp import SalesforceError

    safe = _soql_escape(email)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_RECENT_ACTIVITY_DAYS)).date()

    lead_q = (
        "SELECT Id, Email, HasOptedOutOfEmail, DoNotCall "
        f"FROM Lead WHERE Email = '{safe}'"
    )
    contact_q = (
        "SELECT Id, Email, HasOptedOutOfEmail, DoNotCall, "
        "Account.Type, Account.LastActivityDate "
        f"FROM Contact WHERE Email = '{safe}'"
    )

    try:
        lead_rows = (sf_query(lead_q) or {}).get("records") or []
        contact_rows = (sf_query(contact_q) or {}).get("records") or []
    except SalesforceError as exc:
        log.warning("suppression: SF outage on %s — failing open: %s", email, exc)
        write_audit(
            agent_name="top_of_funnel",
            action="suppression_check_failed",
            target=email,
            after={"error": str(exc)},
        )
        return SuppressionResult(
            suppressed=False, reason=f"sf_outage: {exc}", source="fail_open"
        )

    # Layer 2 — DoNotCall / HasOptedOutOfEmail
    for row in lead_rows + contact_rows:
        if row.get("HasOptedOutOfEmail"):
            return SuppressionResult(True, "HasOptedOutOfEmail=true", "sf_dnc")
        if row.get("DoNotCall"):
            return SuppressionResult(True, "DoNotCall=true", "sf_dnc")

    # Layer 3 — Account.Type = Customer
    for row in contact_rows:
        account = row.get("Account") or {}
        if (account.get("Type") or "").lower() == "customer":
            return SuppressionResult(
                True, f"Account.Type={account.get('Type')}", "sf_customer"
            )

    # Layer 4 — recent activity
    for row in contact_rows:
        account = row.get("Account") or {}
        last = account.get("LastActivityDate")
        if last:
            try:
                last_dt = datetime.strptime(last[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if last_dt >= cutoff:
                return SuppressionResult(
                    True,
                    f"LastActivityDate={last} within {_RECENT_ACTIVITY_DAYS}d",
                    "sf_recent_activity",
                )

    return None


# ------------------------------------------------------------------- public API


async def is_suppressed(
    email: str,
    *,
    domain: str | None = None,
    sf_query: SF_QUERY_CALLABLE | None = None,
    competitor_domains: set[str] | None = None,
    cache_ttl_days: int = _DEFAULT_CACHE_TTL_DAYS,
) -> SuppressionResult:
    """Run the 5-layer suppression check. Writes to cache on fresh checks only."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return SuppressionResult(True, "invalid_email", "input")

    cached = _cache_lookup(email, cache_ttl_days)
    if cached is not None:
        return cached

    if sf_query is None:
        from shared.mcp.salesforce_mcp import soql_query as _default
        sf_query = _default

    sf_hit = _check_salesforce(email, sf_query)
    if sf_hit is not None and sf_hit.suppressed:
        _cache_write(email, sf_hit)
        return sf_hit
    if sf_hit is not None and sf_hit.source == "fail_open":
        # Don't cache fail-open — next run should retry SF.
        return sf_hit

    if competitor_domains is None:
        competitor_domains = _load_competitor_domains()

    resolved_domain = (domain or _domain_from_email(email) or "").lower()
    if resolved_domain and resolved_domain in competitor_domains:
        result = SuppressionResult(True, f"competitor:{resolved_domain}", "competitor")
        _cache_write(email, result)
        return result

    result = SuppressionResult(False, "", "none")
    _cache_write(email, result)
    return result


def _domain_from_email(email: str) -> str | None:
    m = _EMAIL_RE.match(email)
    return m.group(1).lower() if m else None


async def add_manual(email: str, reason: str = "manual") -> dict[str, Any]:
    """Slack entry: `@oo tof suppress <email> [reason]`."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"text": f"`{email}` doesn't look like an email."}

    result = SuppressionResult(True, reason or "manual", "manual")
    _cache_write(email, result)
    write_audit(
        agent_name="top_of_funnel",
        action="suppression_manual_add",
        target=email,
        after={"reason": reason},
    )
    return {"text": f"Suppressed `{email}` (reason: {reason}). Expires in 7 days unless re-added."}
