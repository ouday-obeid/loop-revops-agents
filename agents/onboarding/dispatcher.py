"""Onboarding dispatcher — routes `@oo onboarding <subcommand>` to handlers.

Subcommands (v1):
  ping                           — health check
  help                           — list supported commands
  status <account>               — Onboarding__c snapshot for an account
  stalls [days]                  — stalled onboardings (default ≥5 business days)
  unassigned                     — onboardings with null OwnerId
  stuck-locations [account]      — stuck locations, optionally scoped
  handoff <account>              — run handoff_checklist on demand
  backfill --preview             — count historical Closed Won without Onboarding__c
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text as sql_text

from shared.agent_base import AgentBase
from shared.db.connection import get_engine

log = logging.getLogger(__name__)


HELP_TEXT = (
    "Onboarding agent commands:\n"
    "• `@oo onboarding ping` — health check\n"
    "• `@oo onboarding status <account>` — Onboarding__c snapshot\n"
    "• `@oo onboarding stalls [days]` — stalled onboardings (default ≥5 business days)\n"
    "• `@oo onboarding unassigned` — onboardings with null OwnerId\n"
    "• `@oo onboarding stuck-locations [account]` — stuck locations\n"
    "• `@oo onboarding handoff <account>` — run handoff checklist on demand\n"
    "• `@oo onboarding skip <opp_id> <justification>` — override a blocking handoff item\n"
    "• `@oo onboarding assign <gate_id> <user_id>` — complete an approved CSM reassignment\n"
    "• `@oo onboarding backfill --preview` — count historical Closed Won gaps\n"
    "Alias: `@oo onboarder …` routes here too."
)


class OnboardingDispatcher(AgentBase):
    def __init__(self):
        super().__init__(
            name="onboarding",
            slack_channel="#agent-onboarding-log",
            sf_service_user="revops-agent@tryloop.ai",
            monthly_token_budget=3_000_000,
        )

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        text_in = (payload.get("text") or "").strip()
        if not text_in or text_in.lower() == "ping":
            return {"text": "pong — onboarding online."}

        parts = text_in.split(maxsplit=1)
        cmd = parts[0].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("--help", "-h"):
            cmd = "help"

        if cmd == "help":
            return {"text": HELP_TEXT}
        if cmd == "status":
            return await self._status(rest)
        if cmd in ("stalls", "stall"):
            return await self._stalls(rest)
        if cmd == "unassigned":
            return await self._unassigned()
        if cmd in ("stuck-locations", "stuck_locations", "stuck-locs"):
            return await self._stuck_locations(rest)
        if cmd == "handoff":
            return await self._handoff(rest)
        if cmd == "skip":
            return await self._skip(rest, payload.get("user"))
        if cmd == "assign":
            return await self._assign(rest, payload.get("user"))
        if cmd == "backfill":
            return await self._backfill(rest)

        return {"text": f"Unknown onboarding command: `{cmd}`.\n{HELP_TEXT}"}

    # ---- subcommand handlers ----

    async def _status(self, account: str) -> dict[str, Any]:
        if not account:
            return {"text": "Usage: `@oo onboarding status <account>`"}
        from agents.onboarding import queries
        from shared.mcp import salesforce_mcp

        # Lookup Account by Name (case-insensitive LIKE for convenience).
        safe = account.replace("'", "\\'")
        acc_q = (
            f"SELECT Id, Name FROM Account WHERE Name LIKE '%{safe}%' LIMIT 3"
        )
        acc_res = salesforce_mcp.soql_query(acc_q)
        accs = acc_res.get("records", [])
        if not accs:
            return {"text": f"No account matched `{account}`."}
        account_id = accs[0]["Id"]
        account_name = accs[0]["Name"]

        ob_q = (
            "SELECT Id, Name, JK_Onboarding_Stage__c, Overall_Onboarding_Status__c, "
            "Kickoff_Status__c, Onboarding_Health__c, OwnerId, CSM_2__c, "
            "LastModifiedDate FROM Onboarding__c "
            f"WHERE Opportunity__r.AccountId = '{account_id}' "
            "ORDER BY CreatedDate DESC LIMIT 5"
        )
        ob_res = salesforce_mcp.soql_query(ob_q)
        obs = ob_res.get("records", [])
        if not obs:
            return {"text": f"*{account_name}* — no `Onboarding__c` record found."}

        ob = obs[0]
        lines = [
            f"*{account_name}* — `Onboarding__c` {ob.get('Name', '(unnamed)')}",
            f"• JK Stage: `{ob.get('JK_Onboarding_Stage__c') or '—'}`",
            f"• Overall: `{ob.get('Overall_Onboarding_Status__c') or '—'}`",
            f"• Kickoff: `{ob.get('Kickoff_Status__c') or '—'}`",
            f"• Health: `{ob.get('Onboarding_Health__c') or '—'}`",
            f"• Owner: `{ob.get('OwnerId') or 'UNASSIGNED'}`"
            + (f" / CSM 2: `{ob['CSM_2__c']}`" if ob.get("CSM_2__c") else ""),
            f"• Last modified: `{ob.get('LastModifiedDate') or '—'}`",
        ]
        return {"text": "\n".join(lines)}

    async def _stalls(self, rest: str) -> dict[str, Any]:
        try:
            days = int(rest) if rest else 5
        except ValueError:
            return {"text": f"Usage: `@oo onboarding stalls [days]` (got `{rest}`)"}
        from agents.onboarding import milestone_monitor
        stalls = await milestone_monitor.find_stalls(min_business_days=days)
        if not stalls:
            return {"text": f"No onboardings stalled ≥{days} business days."}
        lines = [f"Stalled onboardings (≥{days} business days):"]
        for s in stalls[:15]:
            lines.append(
                f"• *{s['name']}* — JK: `{s['jk_stage']}` / Overall: `{s['overall']}` "
                f"(owner: `{s.get('owner') or 'UNASSIGNED'}`)"
            )
        if len(stalls) > 15:
            lines.append(f"…and {len(stalls) - 15} more.")
        return {"text": "\n".join(lines)}

    async def _unassigned(self) -> dict[str, Any]:
        from shared.mcp import salesforce_mcp
        q = (
            "SELECT Id, Name, Opportunity__r.Account.Name FROM Onboarding__c "
            "WHERE OwnerId = null AND Overall_Onboarding_Status__c != 'Completed' "
            "ORDER BY CreatedDate DESC LIMIT 25"
        )
        res = salesforce_mcp.soql_query(q)
        rows = res.get("records", [])
        if not rows:
            return {"text": "No unassigned onboardings. Nice."}
        lines = ["Unassigned onboardings (OwnerId null):"]
        for r in rows:
            account = (((r.get("Opportunity__r") or {}).get("Account") or {}).get("Name")
                       or "(no account)")
            lines.append(f"• *{account}* — `{r.get('Name')}` ({r.get('Id')})")
        return {"text": "\n".join(lines)}

    async def _stuck_locations(self, account: str) -> dict[str, Any]:
        from agents.onboarding import location_activation
        result = await location_activation.report(account_filter=account or None)
        return {"text": result}

    async def _handoff(self, account: str) -> dict[str, Any]:
        if not account:
            return {"text": "Usage: `@oo onboarding handoff <account>`"}
        from agents.onboarding import handoff_checklist
        return {"text": await handoff_checklist.run_by_account(account)}

    async def _skip(self, rest: str, user: str | None) -> dict[str, Any]:
        parts = rest.split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            return {"text": (
                "Usage: `@oo onboarding skip <opp_id> <justification>`\n"
                "Justification is required — skip_milestone gates cannot be "
                "created without one."
            )}
        opp_id, justification = parts[0].strip(), parts[1].strip()
        if not opp_id.startswith("006"):
            return {"text": (
                f"`{opp_id}` doesn't look like an Opportunity Id "
                "(expected prefix `006`)."
            )}

        from shared.governance import ApprovalRequired, create_approval_gate
        from shared.mcp import salesforce_mcp

        ob_res = salesforce_mcp.soql_query(
            f"SELECT Id FROM Onboarding__c WHERE Opportunity__c = '{opp_id}' LIMIT 1"
        )
        ob_rows = ob_res.get("records") or []
        onboarding_id = ob_rows[0]["Id"] if ob_rows else None

        try:
            gate_id = create_approval_gate(
                agent_name="onboarding",
                action_type="skip_milestone",
                payload={
                    "opp_id": opp_id,
                    "onboarding_id": onboarding_id,
                    "reason": justification,
                },
                justification=justification,
                requested_by=user or "slack:unknown",
            )
        except ApprovalRequired as exc:
            return {"text": f"Couldn't create skip_milestone gate: {exc}"}

        return {"text": (
            f"skip_milestone gate `{gate_id}` created for opp `{opp_id}`. "
            "Jackie or O approves in their DM.\n"
            f"Justification: _{justification}_"
        )}

    async def _assign(self, rest: str, user: str | None) -> dict[str, Any]:
        parts = rest.split()
        if len(parts) != 2:
            return {"text": (
                "Usage: `@oo onboarding assign <gate_id> <user_id>`\n"
                "Use this after approving a `csm_reassignment` gate to record "
                "the new CSM."
            )}
        gate_raw, new_owner_id = parts
        try:
            gate_id = int(gate_raw)
        except ValueError:
            return {"text": f"`{gate_raw}` is not a valid gate id (expected integer)."}
        if not new_owner_id.startswith("005") or len(new_owner_id) not in (15, 18):
            return {"text": (
                f"`{new_owner_id}` doesn't look like a Salesforce User Id "
                "(expected prefix `005`, length 15 or 18)."
            )}

        from shared.governance import ApprovalRequired
        from agents.onboarding import csm_enforcer

        try:
            result = csm_enforcer.apply_reassignment(
                gate_id,
                new_owner_id=new_owner_id,
                approver=user or "slack:unknown",
            )
        except ApprovalRequired as exc:
            return {"text": f"Gate `{gate_id}` is not ready: {exc}"}
        except ValueError as exc:
            return {"text": f"Gate `{gate_id}` is malformed: {exc}"}

        onboarding_id = result.get("id") or "?"
        return {"text": (
            f"✅ Onboarding `{onboarding_id}` reassigned to `{new_owner_id}` "
            f"(gate `{gate_id}`)."
        )}

    async def _backfill(self, rest: str) -> dict[str, Any]:
        if rest.strip() != "--preview":
            return {
                "text": "Usage: `@oo onboarding backfill --preview` "
                "(only preview is supported; writes are disabled)"
            }
        from shared.mcp import salesforce_mcp
        from agents.onboarding import queries
        res = salesforce_mcp.soql_query(queries.HISTORICAL_CLOSED_WON_WITHOUT_ONBOARDING)
        count = res.get("totalSize")
        if count is None:
            records = res.get("records") or []
            count = records[0].get("expr0") if records else 0
        return {
            "text": f"Backfill preview: *{count}* historical Closed Won opps "
            "without an `Onboarding__c`. No writes performed."
        }


# ---------- Slack registry entry point ----------

async def handle(payload: dict[str, Any]) -> dict[str, Any]:
    return await OnboardingDispatcher().run(trigger="slack", payload=payload)


# ---------- Weekly digest (scheduled Friday 9 AM ET) ----------

async def send_jackie_weekly_digest() -> dict[str, Any]:
    """Compose and send the weekly CS onboarding digest.

    Aggregates three counts from the local audit_log / approval_gates tables
    and forwards a summary Slack message via SlackSender. Dev guard (on by
    default during build) will route the message to SLACK_TEST_CHANNEL.
    """
    from shared.slack_dispatcher import SlackSender
    from shared.secrets import get_config

    engine = get_engine()
    since = datetime.now(timezone.utc) - timedelta(days=7)

    with engine.begin() as conn:
        created = conn.execute(
            sql_text(
                """SELECT COUNT(*) FROM audit_log
                    WHERE agent_name = 'onboarding' AND action = 'sf_create'
                    AND target LIKE 'sf:Onboarding__c%' AND ts >= :since"""
            ),
            {"since": since},
        ).scalar() or 0
        stalls_alerted = conn.execute(
            sql_text(
                """SELECT COUNT(*) FROM audit_log
                    WHERE agent_name = 'onboarding'
                    AND action = 'stall_alert' AND ts >= :since"""
            ),
            {"since": since},
        ).scalar() or 0
        csm_requests = conn.execute(
            sql_text(
                """SELECT COUNT(*) FROM approval_gates
                    WHERE agent_name = 'onboarding'
                    AND action_type = 'csm_reassignment'
                    AND requested_at >= :since"""
            ),
            {"since": since},
        ).scalar() or 0

    text_ = (
        "*Onboarding weekly digest (last 7 days)*\n"
        f"• Onboarding__c records auto-created: *{created}*\n"
        f"• Stall alerts posted: *{stalls_alerted}*\n"
        f"• CSM reassignment requests: *{csm_requests}*\n"
        "_Replies to this thread go to the onboarding log channel._"
    )
    channel = get_config("ONBOARDING_DIGEST_CHANNEL", "#agent-onboarding-log")
    resp = SlackSender().send(channel, text_)
    log.info("jackie weekly digest sent: %s", resp)
    return {"sent": resp, "created": created, "stalls_alerted": stalls_alerted,
            "csm_requests": csm_requests}
