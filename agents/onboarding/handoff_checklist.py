"""Handoff checklist — Sales → Implementation → CS.

Six seed checks per Q3 resolution (2026-04-13). Each returns a tri-valued
result `(status, reason)`:

  True  — pass
  False — hard fail (blocks handoff unless skip_milestone gate is approved)
  None  — informational / not yet applicable (does NOT block)

Jackie refines the seed items during the Phase 3 Week 10 sandbox walkthrough.
Because the checks live in a single list (`CHECKS`), edits are one-file diffs
— no schema or storage changes.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from agents.onboarding import queries
from shared.mcp import salesforce_mcp

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    name: str
    status: bool | None
    reason: str


# ---------- Individual checks ----------

def _count_records(query: str) -> list[dict[str, Any]]:
    res = salesforce_mcp.soql_query(query)
    return res.get("records") or []


def check_products_priced(opp_id: str) -> CheckResult:
    """C6 mitigation: confirm every OpportunityLineItem has UnitPrice set."""
    rows = _count_records(queries.OPP_LINE_ITEMS.format(opp_id=opp_id))
    if not rows:
        return CheckResult(
            "products_priced", False,
            "No OpportunityLineItem rows — products must be attached + priced.",
        )
    missing = [r for r in rows if r.get("UnitPrice") in (None, 0)]
    if missing:
        return CheckResult(
            "products_priced", False,
            f"{len(missing)}/{len(rows)} line items missing UnitPrice.",
        )
    return CheckResult("products_priced", True, f"{len(rows)} products priced.")


def check_stakeholders_captured(opp_id: str) -> CheckResult:
    rows = _count_records(queries.OPP_CONTACT_ROLES.format(opp_id=opp_id))
    if not rows:
        return CheckResult(
            "stakeholders_captured", False,
            "No OpportunityContactRole rows.",
        )
    primaries = [r for r in rows if r.get("IsPrimary")]
    if not primaries:
        return CheckResult(
            "stakeholders_captured", False,
            f"{len(rows)} contact roles but none marked IsPrimary.",
        )
    return CheckResult("stakeholders_captured", True, f"{len(rows)} roles, primary set.")


def check_contract_countersigned(opp_id: str) -> CheckResult:
    """DocuSign_Status__c = 'Completed'. If the field is missing (PandaDoc
    migration) returns None so handoff doesn't block on schema drift."""
    try:
        rows = _count_records(queries.OPP_DOCUSIGN_STATUS.format(opp_id=opp_id))
    except Exception as exc:
        if "DocuSign_Status__c" in str(exc):
            return CheckResult("contract_countersigned", None,
                               "DocuSign_Status__c missing — PandaDoc migration?")
        raise
    if not rows:
        return CheckResult("contract_countersigned", False, "Opportunity not found.")
    status = rows[0].get("DocuSign_Status__c")
    if status == "Completed":
        return CheckResult("contract_countersigned", True, "DocuSign Completed.")
    return CheckResult(
        "contract_countersigned", False,
        f"DocuSign_Status__c = {status or '—'} (need Completed).",
    )


def check_zenskar_billing(opp_id: str) -> CheckResult:
    """Zenskar integration is in-progress per KB. Gated by env flag; OFF by default."""
    if os.getenv("ONBOARDING_ZENSKAR_GATE_ACTIVE", "0") != "1":
        return CheckResult(
            "zenskar_billing", None,
            "Zenskar integration pending — informational only.",
        )
    # When the flag flips, a future commit wires in the real check against
    # whatever SF field / endpoint Zenskar settles on.
    return CheckResult(
        "zenskar_billing", None,
        "Zenskar check active but unimplemented — flag Jackie.",
    )


def check_kickoff_on_calendar(opp_id: str) -> CheckResult:
    rows = _count_records(queries.ONBOARDING_KICKOFF_STATUS.format(opp_id=opp_id))
    if not rows:
        return CheckResult("kickoff_on_calendar", False,
                           "No Onboarding__c record yet.")
    kickoff = rows[0].get("Kickoff_Status__c")
    if kickoff in ("Kickoff Scheduled", "Kickoff Held"):
        return CheckResult("kickoff_on_calendar", True, f"Kickoff = {kickoff}.")
    return CheckResult(
        "kickoff_on_calendar", False,
        f"Kickoff_Status__c = {kickoff or '—'} (need Scheduled or Held).",
    )


def check_implementation_plan_attached(opp_id: str, onboarding_id: str | None) -> CheckResult:
    entity_ids = [opp_id]
    if onboarding_id:
        entity_ids.append(onboarding_id)
    quoted = ", ".join(f"'{i}'" for i in entity_ids)
    rows = _count_records(
        queries.IMPLEMENTATION_PLAN_ATTACHED.format(entity_ids_quoted=quoted)
    )
    if rows:
        return CheckResult("implementation_plan_attached", True,
                           f"{len(rows)} implementation doc(s) linked.")
    return CheckResult(
        "implementation_plan_attached", False,
        "No ContentDocumentLink with 'Implementation' in title.",
    )


# Canonical ordered list. Editing this tuple is the whole refinement story.
# Each entry is (check_name, callable(opp_id, onboarding_id) -> CheckResult).
CHECKS: tuple[tuple[str, Callable[[str, str | None], CheckResult]], ...] = (
    ("products_priced",            lambda opp, ob: check_products_priced(opp)),
    ("stakeholders_captured",      lambda opp, ob: check_stakeholders_captured(opp)),
    ("contract_countersigned",     lambda opp, ob: check_contract_countersigned(opp)),
    ("zenskar_billing",            lambda opp, ob: check_zenskar_billing(opp)),
    ("kickoff_on_calendar",        lambda opp, ob: check_kickoff_on_calendar(opp)),
    ("implementation_plan_attached",
     lambda opp, ob: check_implementation_plan_attached(opp, ob)),
)


# ---------- Runners ----------

def run(opp_id: str, onboarding_id: str | None = None) -> list[CheckResult]:
    results: list[CheckResult] = []
    for name, fn in CHECKS:
        try:
            results.append(fn(opp_id, onboarding_id))
        except Exception as exc:
            log.exception("handoff check %s failed: %s", name, exc)
            results.append(CheckResult(name, None, f"check errored: {exc}"))
    return results


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    passed = [r for r in results if r.status is True]
    failed = [r for r in results if r.status is False]
    info = [r for r in results if r.status is None]
    return {
        "total": len(results),
        "passed": len(passed),
        "failed": len(failed),
        "informational": len(info),
        "blocking": len(failed),
        "all_pass": not failed,
    }


def format_slack(account: str, results: list[CheckResult]) -> str:
    s = summarize(results)
    head = [
        f"*Handoff checklist — {account}*",
        f"{s['passed']}/{s['total']} passed · "
        f"{s['failed']} failed · {s['informational']} informational",
        "",
    ]
    for r in results:
        emoji = {True: "✅", False: "❌", None: "➖"}[r.status]
        head.append(f"{emoji} *{r.name}* — {r.reason}")
    if not s["all_pass"] and s["failed"]:
        head.append(
            "\n_Blockers can be overridden with_ `@oo onboarding skip <opp_id>` "
            "_(requires justification; gate = skip_milestone)._"
        )
    return "\n".join(head)


async def run_by_account(account: str) -> str:
    """Dispatcher helper — look up the most recent opp+onboarding for an
    account name and run the checklist."""
    safe = account.replace("'", "\\'")
    acc_res = salesforce_mcp.soql_query(
        f"SELECT Id, Name FROM Account WHERE Name LIKE '%{safe}%' LIMIT 3"
    )
    accs = acc_res.get("records") or []
    if not accs:
        return f"No account matched `{account}`."
    account_id = accs[0]["Id"]
    account_name = accs[0]["Name"]

    opp_res = salesforce_mcp.soql_query(
        "SELECT Id, Name FROM Opportunity "
        f"WHERE AccountId = '{account_id}' AND StageName = 'Closed Won' "
        "ORDER BY CloseDate DESC LIMIT 1"
    )
    opps = opp_res.get("records") or []
    if not opps:
        return f"*{account_name}* has no Closed Won opportunity."
    opp = opps[0]

    ob_res = salesforce_mcp.soql_query(
        "SELECT Id FROM Onboarding__c "
        f"WHERE Opportunity__c = '{opp['Id']}' LIMIT 1"
    )
    ob_rows = ob_res.get("records") or []
    onboarding_id = ob_rows[0]["Id"] if ob_rows else None

    results = run(opp["Id"], onboarding_id)
    return format_slack(account_name, results)
