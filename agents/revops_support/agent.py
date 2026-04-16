"""RevOps Support dispatcher — routes @oo revops-support <cmd> subcommands.

Phase 1 Week 1 surface:
  help, ping, soql, pipeline, stale, tlos, opps-missing-products,
  accounts-no-tlo, dup-contacts, active-users, validation-rules

Later weeks wire in:
  schema (propose/test/deploy/rollback), data-quality (dedup/bulk-fix),
  permissions (provision/offboard/grant), integration-health, knowledge
"""
from __future__ import annotations

import logging
import shlex
from typing import Any, Awaitable, Callable

from shared.agent_base import AgentBase

from agents.revops_support.data_quality import validation_monitor
from agents.revops_support.query import canned, soql_engine

log = logging.getLogger(__name__)

HELP_TEXT = (
    "RevOps Support commands:\n"
    "• `@oo revops-support ping` — health check\n"
    "• `@oo revops-support soql <query>` — run read-only SOQL\n"
    "• `@oo revops-support pipeline by stage` — open opps grouped by stage\n"
    "• `@oo revops-support stale opportunities [days]` — opps stale > N days (default 30)\n"
    "• `@oo revops-support tlos with no opps` — TLOs with zero opportunities\n"
    "• `@oo revops-support opps missing products` — Closed Won without line items\n"
    "• `@oo revops-support accounts with no tlo` — accounts missing TLO linkage\n"
    "• `@oo revops-support duplicate contacts` — emails with >1 contact\n"
    "• `@oo revops-support active users` — users with login in last 30 days\n"
    "• `@oo revops-support validation rules <ObjectName>` — active rules for object\n"
    "• `@oo revops-support validation monitor` — org-wide validation-rule health check\n"
    "• `@oo revops-support help` — this message\n"
    "Alias: `@oo admin …` routes here too."
)


class RevOpsSupportAgent(AgentBase):
    def __init__(self) -> None:
        super().__init__(
            name="revops_support",
            slack_channel="#agent-revops-support-log",
            monthly_token_budget=8_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        text_in = (payload.get("text") or "").strip()
        if not text_in or text_in.lower() == "ping":
            return {"text": "pong — RevOps Support online."}

        lower = text_in.lower()
        if lower in ("help", "--help", "-h"):
            return {"text": HELP_TEXT}

        # Phrase-based matching for natural commands. Order matters — more
        # specific phrases first.
        try:
            if lower == "soql" or lower.startswith("soql "):
                return await self._soql(text_in[5:].strip() if len(text_in) > 4 else "")
            if "pipeline" in lower and "stage" in lower:
                return _run_canned(canned.pipeline_by_stage)
            if lower.startswith("stale"):
                return _run_canned(canned.stale_opportunities, _extract_days(lower, 30))
            if "tlo" in lower and "no opp" in lower:
                return _run_canned(canned.tlos_with_no_opps)
            if "missing product" in lower:
                return _run_canned(canned.opps_missing_products)
            if "account" in lower and "no tlo" in lower:
                return _run_canned(canned.accounts_with_no_tlo)
            if "duplicate contact" in lower or "dup contact" in lower:
                return _run_canned(canned.duplicate_contacts_by_email)
            if "active user" in lower:
                return _run_canned(canned.active_users_with_login, _extract_days(lower, 30))
            if "validation monitor" in lower or lower == "validation":
                return _format_validation_monitor(validation_monitor.poll())
            if "validation rule" in lower:
                obj = _extract_object(text_in)
                if not obj:
                    return {"text": "Usage: `@oo revops-support validation rules <ObjectName>`"}
                return _run_canned(canned.validation_rule_violations, obj)
        except soql_engine.SOQLError as e:
            return {"text": f"SOQL error: {e}"}

        # Future: schema, dedup, permissions, health, knowledge
        if lower.split()[0] in _FUTURE_COMMANDS:
            return {"text": f"`{lower.split()[0]}` ships later in Phase 1. Not yet wired."}

        return {"text": f"Unknown revops-support command.\n{HELP_TEXT}"}

    async def _soql(self, query: str) -> dict[str, Any]:
        if not query:
            return {"text": "Usage: `@oo revops-support soql <SELECT ...>`"}
        try:
            result = soql_engine.run(query, agent_name=self.name)
        except soql_engine.SOQLError as e:
            return {"text": f"SOQL rejected: {e}"}
        records = result.get("records", [])
        if not records:
            return {"text": f"0 rows.\n`{query[:200]}`"}
        return {
            "text": f"{len(records)} rows. Sample:\n```{records[0]}```",
            "records": records,
        }


_FUTURE_COMMANDS = {
    "schema",
    "dedup",
    "provision",
    "offboard",
    "grant",
    "deploy",
    "rollback",
    "knowledge",
    "bulk",
}


def _extract_days(text_in: str, default: int) -> int:
    for tok in text_in.split():
        if tok.isdigit():
            return int(tok)
    return default


def _extract_object(text_in: str) -> str | None:
    # Grab the last token that looks like an SObject API name (CamelCase or __c).
    try:
        toks = shlex.split(text_in)
    except ValueError:
        toks = text_in.split()
    for tok in reversed(toks):
        if tok.endswith("__c") or (tok[:1].isupper() and tok.isalnum()):
            return tok
    return None


def _run_canned(fn: Callable[..., dict[str, Any]], *args: Any) -> dict[str, Any]:
    result = fn(*args)
    return {
        "text": result["text"],
        "blocks": result.get("blocks", []),
        "records": result.get("records", []),
    }


def _format_validation_monitor(result: dict[str, Any]) -> dict[str, Any]:
    summary = result.get("summary", {})
    flagged = result.get("flagged", [])
    task_ids = result.get("task_ids", [])
    lines = [
        "*Validation Rule Health*",
        f"• Total active: {summary.get('total', 0)}",
        f"• Orphaned: {len(result.get('orphans', []))}",
        f"• Stale: {len(result.get('stale', []))}",
        f"• Tasks created: {len(task_ids)}",
    ]
    if flagged:
        sample = flagged[:5]
        lines.append("_Top issues:_")
        for r in sample:
            lines.append(
                f"  · {r.get('object')}.{r.get('name')} — {r.get('issue')}"
            )
        if len(flagged) > len(sample):
            lines.append(f"  …and {len(flagged) - len(sample)} more")
    return {"text": "\n".join(lines), "result": result}


async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    return await RevOpsSupportAgent().run(trigger="slack", payload=payload)
