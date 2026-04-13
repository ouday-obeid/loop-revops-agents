"""AgentBase — lifecycle, logging, approval + rate-limit + audit helpers.

Every specialist subclasses this. MCPs are injected lazily so tests can mock.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from shared import governance
from shared.db.connection import get_engine

log = logging.getLogger(__name__)

# Rough pricing — adjust for actual model later (claude-opus-4-6 ~ $15 in / $75 out per MTok)
_INPUT_PER_MTOK = 15.0
_OUTPUT_PER_MTOK = 75.0


@dataclass
class RunContext:
    run_id: int
    started_at: float
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass
class AgentBase:
    name: str
    slack_channel: str = ""
    sf_service_user: str | None = None
    monthly_token_budget: int = 5_000_000
    _mcps: dict[str, Any] = field(default_factory=dict)

    # ---- Lifecycle ----
    def on_start(self) -> None:
        log.info("agent %s starting", self.name)

    def on_shutdown(self) -> None:
        log.info("agent %s shutting down", self.name)

    # ---- MCP injection ----
    def attach(self, **mcps: Any) -> None:
        self._mcps.update(mcps)

    @property
    def sf(self):
        return self._mcps.get("sf")

    @property
    def fireflies(self):
        return self._mcps.get("fireflies")

    @property
    def knowledge(self):
        return self._mcps.get("knowledge")

    @property
    def slack(self):
        return self._mcps.get("slack")

    # ---- Run wrapper ----
    async def run(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        ctx = self._start_run(trigger, payload)
        try:
            result = await self.handle(trigger, payload)
            self._finish_run(ctx, status="success", output=result)
            return result
        except Exception as e:
            log.exception("agent %s failed", self.name)
            self._finish_run(ctx, status="error", output=None, error=str(e))
            raise

    async def handle(self, trigger: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Override in subclasses."""
        return {"ok": True, "trigger": trigger}

    # ---- Governance helpers ----
    async def request_approval(
        self, action_type: str, payload: dict[str, Any], justification: str | None = None
    ) -> int:
        return governance.create_approval_gate(
            agent_name=self.name,
            action_type=action_type,
            payload=payload,
            justification=justification,
        )

    async def check_rate_limit(self, bucket: str) -> int:
        return governance.check_rate_limit(bucket)

    async def log_audit(
        self,
        action: str,
        target: str | None = None,
        before: dict | None = None,
        after: dict | None = None,
        approval_gate_id: int | None = None,
    ) -> None:
        governance.write_audit(
            agent_name=self.name,
            action=action,
            target=target,
            before=before,
            after=after,
            approval_gate_id=approval_gate_id,
        )

    # ---- Internals ----
    def _start_run(self, trigger: str, payload: dict) -> RunContext:
        engine = get_engine()
        with engine.begin() as conn:
            res = conn.execute(
                text(
                    """INSERT INTO agent_runs (agent_name, trigger, input, status, started_at)
                       VALUES (:a, :t, :i, 'running', :now)"""
                ),
                {
                    "a": self.name,
                    "t": trigger,
                    "i": json.dumps(payload)[:4000],
                    "now": datetime.now(timezone.utc),
                },
            )
            run_id = res.lastrowid
            if run_id is None:
                run_id = conn.execute(
                    text("SELECT id FROM agent_runs ORDER BY id DESC LIMIT 1")
                ).scalar()
        return RunContext(run_id=int(run_id), started_at=time.monotonic())

    def _finish_run(
        self,
        ctx: RunContext,
        *,
        status: str,
        output: Any,
        error: str | None = None,
    ) -> None:
        duration = int((time.monotonic() - ctx.started_at) * 1000)
        tokens = ctx.tokens_in + ctx.tokens_out
        cost = (ctx.tokens_in / 1_000_000) * _INPUT_PER_MTOK + (
            ctx.tokens_out / 1_000_000
        ) * _OUTPUT_PER_MTOK
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(
                text(
                    """UPDATE agent_runs
                         SET status = :s, duration_ms = :d, tokens_used = :tok,
                             cost_usd = :c, output = :o, error_message = :e,
                             completed_at = :now
                       WHERE id = :id"""
                ),
                {
                    "s": status,
                    "d": duration,
                    "tok": tokens,
                    "c": cost,
                    "o": json.dumps(output)[:4000] if output is not None else None,
                    "e": error,
                    "now": datetime.now(timezone.utc),
                    "id": ctx.run_id,
                },
            )
