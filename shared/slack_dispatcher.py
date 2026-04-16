"""Slack dispatcher — Bolt Socket Mode, routing, approval buttons, dev guard.

Routes @oo mentions and DMs to registered handlers. Approval buttons update
approval_gates rows. DEV guard refuses to send to any channel/user other than
SLACK_TEST_CHANNEL when SLACK_DEV_GUARD=1.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from shared.secrets import get_config, require_secret

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ActionHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]
_registry: dict[str, Handler] = {}
_action_handlers: dict[str, ActionHandler] = {}

# Human persona names that resolve to canonical agent registry keys. Applied in
# parse_command() before the registry lookup, so `@oo outbounder enrich` routes
# to the same handler as `@oo top_of_funnel enrich`. Directories, DB identifiers,
# Slack log channels, and Monday cards are unchanged — this is dispatcher sugar.
PERSONA_ALIASES: dict[str, str] = {
    "outbounder": "top_of_funnel",
    "closer": "sales_reps",
    "onboarder": "onboarding",
    "supporter": "cs",
    "admin": "revops_support",
    "urkel": "slt_metrics",
}


def register(agent_name: str, handler: Handler) -> None:
    _registry[agent_name] = handler


def register_action(action_id: str, handler: ActionHandler) -> None:
    """Register a Slack block action handler (e.g. button clicks).

    Handlers receive the Bolt `body` dict and may return a dict containing
    `text` (posted back to the triggering channel). Returning None suppresses
    the reply.
    """
    _action_handlers[action_id] = handler


def _dev_guard_blocks(target: str) -> bool:
    if get_config("SLACK_DEV_GUARD", "1") != "1":
        return False
    allowed = get_config("SLACK_TEST_CHANNEL", "")
    return bool(allowed) and target != allowed


def _dev_guard_redirect_target() -> str | None:
    """Return the channel to redirect to when dev guard is active."""
    if get_config("SLACK_DEV_GUARD", "1") != "1":
        return None
    return get_config("SLACK_TEST_CHANNEL", "") or None


def parse_command(text_in: str) -> tuple[str | None, str]:
    """@oo <agent> <rest>  |  @oo <rest>  →  (agent|None, rest)."""
    cleaned = re.sub(r"<@[A-Z0-9]+>", "", text_in).strip()
    if cleaned.lower().startswith("oo "):
        cleaned = cleaned[3:].strip()
    parts = cleaned.split(maxsplit=1)
    if not parts:
        return None, ""
    first = PERSONA_ALIASES.get(parts[0].lower(), parts[0].lower())
    if first in _registry and first != "oo":
        return first, parts[1] if len(parts) > 1 else ""
    return None, cleaned


async def dispatch(text_in: str, context: dict[str, Any]) -> dict[str, Any]:
    agent, rest = parse_command(text_in)
    target = agent or "oo"
    handler = _registry.get(target)
    if handler is None:
        return {"error": f"no handler for {target}"}
    return await handler({"text": rest, **context})


class SlackSender:
    """Thin wrapper around Bolt's client with dev guard + audit hooks."""

    def __init__(self, client: Any | None = None):
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            from slack_sdk import WebClient  # lazy import
            self._client = WebClient(token=require_secret("SLACK_BOT_TOKEN"))
        return self._client

    def send(self, channel: str, text_: str, blocks: list | None = None) -> dict[str, Any]:
        redirect = _dev_guard_redirect_target() if _dev_guard_blocks(channel) else None
        if redirect:
            log.info("DEV GUARD redirecting send %s → %s", channel, redirect)
            text_ = f"_[dev-guard → `{channel}`]_\n{text_}"
            channel = redirect
        client = self._ensure_client()
        resp = client.chat_postMessage(channel=channel, text=text_, blocks=blocks)
        return {"ok": resp["ok"], "ts": resp.get("ts"), "channel": resp.get("channel")}

    def ping_o_dm(self) -> dict[str, Any]:
        """Post a one-line liveness ping to O's DM. Used by infra/bootstrap.sh
        to verify SLACK_BOT_TOKEN is wired before the daemon comes up."""
        target = get_config("SLACK_TEST_CHANNEL") or "U07P4GX9YLQ"
        return self.send(target, ":wave: bootstrap ping — Slack bot token wired correctly")


def approval_blocks(gate_id: int, action_type: str, summary: str) -> list[dict[str, Any]]:
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Approval needed: {action_type}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {
            "type": "actions",
            "block_id": f"gate_{gate_id}",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"},
                 "style": "primary", "action_id": "approve_gate", "value": str(gate_id)},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"},
                 "style": "danger", "action_id": "reject_gate", "value": str(gate_id)},
            ],
        },
    ]


def handle_gate_decision(gate_id: int, approved: bool, approver: str) -> None:
    """Thin wrapper around governance.decide_approval_gate with cooldown guard.

    Defense-in-depth for dual-approval-cooldown flows (`sf_schema_delete`):
    before forwarding an approve click to governance, verify that a child
    confirm-gate's parent cooldown_until has actually elapsed. Even if the
    cooldown poller has a bug that creates the confirm gate early, an
    approver hitting the Slack button inside the 24h window is refused at
    the dispatcher layer. Rejections always pass through unmodified — the
    operator can always withdraw approval regardless of cooldown state.

    Routing through governance (vs raw SQL) still means dual-approval,
    cooldown promotion, and gate_decided audit logging happen automatically.
    Kept as a wrapper because external callers (csm_enforcer, button
    handlers, regression tests) import it.
    """
    from shared import governance

    if approved:
        gate = governance.get_approval_gate(gate_id)
        parent_id = gate.get("parent_gate_id") if gate else None
        if parent_id:
            parent = governance.get_approval_gate(parent_id)
            cooldown_until = parent.get("cooldown_until") if parent else None
            if cooldown_until is not None:
                if isinstance(cooldown_until, str):
                    cooldown_until = datetime.fromisoformat(
                        cooldown_until.replace("Z", "+00:00")
                    )
                if cooldown_until.tzinfo is None:
                    cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < cooldown_until:
                    log.warning(
                        "handle_gate_decision: refusing approve on gate %s — "
                        "parent gate %s cooldown_until=%s has not elapsed",
                        gate_id, parent_id, cooldown_until,
                    )
                    raise governance.ApprovalRequired(
                        f"gate {gate_id} confirm refused: parent cooldown "
                        f"until {cooldown_until.isoformat()} has not elapsed"
                    )

    governance.decide_approval_gate(gate_id, approved=approved, approver=approver)


def build_app() -> Any:
    """Lazy Bolt app construction — only when running the daemon."""
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=require_secret("SLACK_BOT_TOKEN"))

    @app.event("app_mention")
    async def _on_mention(event, say):
        result = await dispatch(event.get("text", ""), {"user": event.get("user"), "channel": event.get("channel"), "thread_ts": event.get("ts")})
        await say(text=_render(result), thread_ts=event.get("ts"))

    @app.event("message")
    async def _on_dm(event, say):
        if event.get("channel_type") != "im":
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        result = await dispatch(
            event.get("text", ""),
            {"user": event.get("user"), "channel": event.get("channel"), "thread_ts": thread_ts},
        )
        await say(text=_render(result), thread_ts=thread_ts)

    @app.action("approve_gate")
    async def _approve(ack, body, client):
        await ack()
        gate_id = int(body["actions"][0]["value"])
        handle_gate_decision(gate_id, True, body["user"]["id"])
        await client.chat_postMessage(channel=body["channel"]["id"], text=f"✅ gate {gate_id} approved")

    @app.action("reject_gate")
    async def _reject(ack, body, client):
        await ack()
        gate_id = int(body["actions"][0]["value"])
        handle_gate_decision(gate_id, False, body["user"]["id"])
        await client.chat_postMessage(channel=body["channel"]["id"], text=f"❌ gate {gate_id} rejected")

    for action_id, handler in _action_handlers.items():
        _register_block_action(app, action_id, handler)

    return app


def _register_block_action(app: Any, action_id: str, handler: ActionHandler) -> None:
    @app.action(action_id)
    async def _on_action(ack, body, client):  # pragma: no cover - Bolt runtime
        await ack()
        result = await handler(body)
        if isinstance(result, dict) and result.get("text"):
            channel = (body.get("channel") or {}).get("id")
            if channel:
                await client.chat_postMessage(channel=channel, text=result["text"])


def _render(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, dict) and "text" in result:
        return result["text"]
    return f"```{json.dumps(result, indent=2, default=str)[:2500]}```"


async def run_socket_mode() -> None:  # pragma: no cover - runtime only
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    app = build_app()
    handler = AsyncSocketModeHandler(app, require_secret("SLACK_APP_TOKEN"))
    await handler.start_async()
