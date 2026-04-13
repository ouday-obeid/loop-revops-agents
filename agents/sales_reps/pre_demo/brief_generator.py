"""Pre-demo brief composer.

`generate(target)` accepts either a Salesforce Opportunity Id (prefix `006`)
or a free-text account name / company domain. It assembles a brief from
five sources, every one of which is best-effort (a failure produces a
smaller brief, never an exception):

  1. Salesforce — Opportunity + Account (stage, amount, close, owner).
  2. Fireflies — last 3 transcripts with this account's external attendees.
  3. Knowledge base — semantic search over prior account notes.
  4. Apollo / web — recent news + funding rounds.
  5. Clay — decision-maker enrichment for known attendees + committee.

Output shape is serializable (dicts/lists/primitives) so the formatter
can render either Slack text or Block Kit without re-parsing.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from shared import governance
from shared.mcp import fireflies_mcp, knowledge_mcp, salesforce_mcp

from agents.sales_reps.pre_demo import formatter, linkedin_enrichment

log = logging.getLogger(__name__)

_AGENT_NAME = "sales_reps"
_INTERNAL_DOMAIN = "@tryloop.ai"
_OPP_ID_PATTERN = re.compile(r"^006[a-zA-Z0-9]{12,15}$")


# --------------------------------------------------------------- SF resolve

def _resolve_opportunity(target: str) -> dict[str, Any] | None:
    """Resolve a target string to an SF Opportunity record."""
    if _OPP_ID_PATTERN.match(target):
        q = (
            "SELECT Id, Name, StageName, Amount, CloseDate, IsClosed, "
            "AccountId, Account.Name, Account.Website, Owner.Email "
            f"FROM Opportunity WHERE Id = '{target}'"
        )
        rows = _safe_soql(q).get("records", []) or []
        return rows[0] if rows else None

    # Treat as account name — pick the most recently modified open Opp.
    escaped = target.replace("'", "\\'")
    q = (
        "SELECT Id, Name, StageName, Amount, CloseDate, IsClosed, "
        "AccountId, Account.Name, Account.Website, Owner.Email "
        f"FROM Opportunity WHERE Account.Name LIKE '%{escaped}%' "
        "AND IsClosed = false "
        "ORDER BY LastModifiedDate DESC"
    )
    rows = _safe_soql(q, limit=1).get("records", []) or []
    return rows[0] if rows else None


def _list_account_contact_emails(account_id: str | None) -> list[str]:
    if not account_id:
        return []
    q = f"SELECT Email FROM Contact WHERE AccountId = '{account_id}' AND Email != null"
    rows = _safe_soql(q, limit=20).get("records", []) or []
    return [
        (r.get("Email") or "").lower()
        for r in rows
        if r.get("Email") and not (r["Email"].lower().endswith(_INTERNAL_DOMAIN))
    ]


def _safe_soql(query: str, *, limit: int = 5) -> dict[str, Any]:
    try:
        return salesforce_mcp.soql_query(query, limit=limit) or {}
    except Exception as e:  # noqa: BLE001 — partial brief > no brief
        log.warning("brief: SOQL failed (%s): %s", query[:80], e)
        return {}


# --------------------------------------------------------------- Fireflies

def _prior_calls(emails: list[str], *, limit: int = 3) -> list[dict[str, Any]]:
    if not emails:
        return []
    out: list[dict[str, Any]] = []
    for email in emails[:3]:
        try:
            rows = fireflies_mcp.list_transcripts(participant_email=email, limit=limit)
        except Exception as e:  # noqa: BLE001
            log.warning("fireflies list failed for %s: %s", email, e)
            continue
        for r in rows:
            if r.get("id") and r["id"] not in {x.get("id") for x in out}:
                out.append({
                    "id": r["id"],
                    "title": r.get("title"),
                    "date": r.get("date"),
                    "duration": r.get("duration"),
                    "host_email": r.get("host_email"),
                })
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:limit]


# --------------------------------------------------------------- KB + web

def _knowledge_hits(account_name: str | None) -> list[dict[str, Any]]:
    if not account_name:
        return []
    try:
        hits = knowledge_mcp.semantic_search(account_name, corpus="sales_reps_accounts", k=3)
    except Exception as e:  # noqa: BLE001
        log.warning("knowledge search failed for %s: %s", account_name, e)
        return []
    return [
        {"snippet": (h.get("content") or "")[:500], "score": h.get("score"),
         "metadata": h.get("metadata") or {}}
        for h in hits
    ]


def _news_and_funding(domain: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not domain:
        return [], []
    # Lazy import — web_research loads httpx only if called.
    from agents.sales_reps.integrations import web_research
    try:
        news = web_research.fetch_company_news(domain, limit=5)
    except Exception as e:  # noqa: BLE001
        log.warning("news fetch failed for %s: %s", domain, e)
        news = []
    try:
        funding = web_research.fetch_funding_events(domain)
    except Exception as e:  # noqa: BLE001
        log.warning("funding fetch failed for %s: %s", domain, e)
        funding = []
    return news, funding


# --------------------------------------------------------------- helpers

def _domain_from_website(website: str | None) -> str | None:
    if not website:
        return None
    w = website.strip()
    if "://" not in w:
        w = "https://" + w
    try:
        host = urlparse(w).netloc.lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _derive_talking_points(
    opp: dict[str, Any],
    prior_calls: list[dict[str, Any]],
    funding: list[dict[str, Any]],
) -> list[str]:
    pts: list[str] = []
    stage = (opp.get("StageName") or "").lower()
    if "discovery" in stage or "qualification" in stage:
        pts.append("Confirm budget cycle and decision timeline — still early-stage.")
    if "proposal" in stage or "negotiation" in stage:
        pts.append("Review any outstanding objections from the last call.")
    if prior_calls:
        last = prior_calls[0]
        pts.append(f"Follow up on action items from {last.get('title') or 'last call'}.")
    if funding:
        r = funding[0]
        amt = r.get("amount_usd")
        amt_txt = f"${amt:,.0f}" if amt else r.get("type") or "recent funding"
        pts.append(f"Congratulate on {amt_txt} ({r.get('announced_at') or 'recent'}).")
    return pts


def _derive_gaps(
    opp: dict[str, Any],
    people: list[dict[str, Any]],
) -> list[str]:
    gaps: list[str] = []
    if not opp.get("Amount"):
        gaps.append("Amount not set — confirm deal size on the call.")
    if not opp.get("CloseDate"):
        gaps.append("CloseDate not set — align on target signature date.")
    attending = [p for p in people if p.get("attending")]
    if len(attending) < 2:
        gaps.append("Single-threaded — try to get a second attendee on future calls.")
    titled = [p for p in people if p.get("title")]
    if not titled:
        gaps.append("No decision-maker titles enriched — manual LinkedIn pass may be needed.")
    return gaps


# --------------------------------------------------------------- public API

async def generate(target: str, *, include_blocks: bool = False) -> dict[str, Any]:
    """Compose a pre-demo brief for an Opportunity Id or account name."""
    target = (target or "").strip()
    if not target:
        return {"text": "Usage: `@oo sales-reps brief <opp_id|account_name>`", "error": "empty_target"}

    opp = _resolve_opportunity(target)
    if not opp:
        return {
            "text": f"Pre-demo brief: no open Opportunity matches `{target}`.",
            "error": "not_found",
            "target": target,
        }

    account = opp.get("Account") or {}
    account_name = account.get("Name")
    domain = _domain_from_website(account.get("Website"))
    account_id = opp.get("AccountId")

    attendee_emails = _list_account_contact_emails(account_id)
    people = linkedin_enrichment.enrich_for_brief(domain, attendee_emails[:5])

    prior_calls = _prior_calls(attendee_emails)
    knowledge = _knowledge_hits(account_name)
    news, funding = _news_and_funding(domain)

    brief = {
        "target": target,
        "opportunity_id": opp.get("Id"),
        "opportunity_name": opp.get("Name"),
        "account_id": account_id,
        "account_name": account_name,
        "domain": domain,
        "stage": opp.get("StageName"),
        "amount": opp.get("Amount"),
        "close_date": opp.get("CloseDate"),
        "owner_email": ((opp.get("Owner") or {}).get("Email") or "").lower() or None,
        "people": people,
        "prior_calls": prior_calls,
        "knowledge": knowledge,
        "news": news,
        "funding": funding,
    }
    brief["talking_points"] = _derive_talking_points(opp, prior_calls, funding)
    brief["gaps"] = _derive_gaps(opp, people)
    brief["text"] = formatter.to_slack_text(brief)
    if include_blocks:
        brief["blocks"] = formatter.to_slack_blocks(brief)

    governance.write_audit(
        agent_name=_AGENT_NAME,
        action="sales_reps_pre_demo_brief",
        target=f"sf:Opportunity:{opp.get('Id')}",
        after={
            "people": len(people),
            "prior_calls": len(prior_calls),
            "knowledge_hits": len(knowledge),
            "news": len(news),
            "funding": len(funding),
        },
    )
    return brief
