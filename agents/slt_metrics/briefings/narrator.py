"""ClaudeRouter — model tiering + budget-aware downshift.

Routes each narrative kind to the cheapest model that still does the job,
and downshifts Opus → Sonnet once month-to-date token usage crosses the
90% threshold of `SltMetricsAgent.monthly_token_budget`.

Anthropic is imported lazily inside `_call_model` so tests that never hit
the network don't require `ANTHROPIC_API_KEY`. Without a key, `narrate`
returns the fallback text unchanged — callers always get a string back.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Callable

from sqlalchemy import text

from shared.db.connection import get_engine
from shared.secrets import get_config

log = logging.getLogger(__name__)


# Model IDs — kept as strings so tests can swap them via monkeypatch if the
# scoping doc's preferred IDs ever change.
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS = "claude-opus-4-6"
MODEL_HAIKU = "claude-haiku-4-5-20251001"


# kind → (model, max_tokens). See plan's "Model-tier / cost routing" table.
ROUTING: dict[str, tuple[str, int]] = {
    "daily_briefing":       (MODEL_SONNET, 1_200),
    "mover_wrap":           (MODEL_OPUS,   1_500),
    "friday_wrap":          (MODEL_OPUS,   2_500),
    "champion_classifier":  (MODEL_HAIKU,  300),
    "backtest_report":      (MODEL_SONNET, 2_000),
    "adhoc_slt":            (MODEL_SONNET, 1_000),
    "assembly":             (MODEL_SONNET, 1_500),
}

# Budget threshold at which Opus auto-downshifts to Sonnet.
BUDGET_DOWNSHIFT_PCT = 0.90


class ClaudeRouter:
    """Selects a Claude model per narrative kind and calls it.

    Construct with an injected `client_factory` to use a test double; in
    production, the lazy `anthropic.Anthropic(api_key=...)` import applies.
    """

    def __init__(
        self,
        *,
        agent_name: str = "slt_metrics",
        monthly_budget: int = 5_000_000,
        client_factory: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.agent_name = agent_name
        self.monthly_budget = monthly_budget
        self._client_factory = client_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------- selection

    def select_model(self, kind: str) -> str:
        if kind not in ROUTING:
            raise ValueError(f"Unknown narrative kind: {kind!r}")
        model, _ = ROUTING[kind]
        used = self.month_to_date_tokens()
        if model == MODEL_OPUS and used >= BUDGET_DOWNSHIFT_PCT * self.monthly_budget:
            log.warning(
                "Budget at %.0f%% of %d tokens — downshifting %s from %s to %s",
                BUDGET_DOWNSHIFT_PCT * 100, self.monthly_budget, kind, MODEL_OPUS, MODEL_SONNET,
            )
            return MODEL_SONNET
        return model

    def month_to_date_tokens(self) -> int:
        engine = get_engine()
        now = self._clock()
        month_start = date(now.year, now.month, 1).isoformat()
        with engine.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT COALESCE(SUM(tokens_used), 0) FROM agent_runs "
                    "WHERE agent_name = :a AND started_at >= :m"
                ),
                {"a": self.agent_name, "m": month_start},
            ).scalar()
        return int(row or 0)

    # ------------------------------------------------------------- narration

    def narrate(
        self,
        kind: str,
        *,
        system: str,
        user: str,
        fallback: str,
        max_tokens: int | None = None,
    ) -> str:
        """Run a Claude completion for `kind`; return the generated text or `fallback`.

        * No API key → `fallback` (no network call).
        * API error → `fallback` (logged) so briefings never hard-fail.
        """
        api_key = get_config("ANTHROPIC_API_KEY")
        if not api_key:
            log.info("No ANTHROPIC_API_KEY — narrator returning fallback for %s", kind)
            return fallback

        model = self.select_model(kind)
        tokens = max_tokens or ROUTING[kind][1]
        try:
            return self._call_model(model, system=system, user=user, max_tokens=tokens)
        except Exception as e:  # noqa: BLE001 — narration must never break a briefing
            log.warning("Claude call failed (%s/%s): %s — using fallback", kind, model, e)
            return fallback

    def _call_model(self, model: str, *, system: str, user: str, max_tokens: int) -> str:
        client = self._client_factory() if self._client_factory else self._default_client()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in resp.content if hasattr(b, "text")).strip()

    def _default_client(self):
        from anthropic import Anthropic  # lazy — only when a real call fires
        api_key = get_config("ANTHROPIC_API_KEY")
        return Anthropic(api_key=api_key)
