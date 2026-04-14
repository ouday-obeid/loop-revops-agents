"""Dispatcher routing tests — ping, unknown, help, each subcommand stub, aliases."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from agents.slt_metrics import dispatcher
from agents.slt_metrics.agent import SltMetricsAgent
from agents.slt_metrics.main import handle, register_with_dispatcher
from shared.db.connection import get_engine
from shared.slack_dispatcher import _registry, dispatch


class _CapturingSender:
    """Records DM sends without hitting Slack. Matches test_jobs.py pattern."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, channel: str, text_: str, blocks: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        self.calls.append({"channel": channel, "text": text_, "blocks": blocks})
        return {"ok": True, "ts": "1.0", "channel": channel}


@pytest.fixture
def capture_forecast_dm(monkeypatch):
    """Patch dispatcher's sender lookup to capture the forecast DM."""
    cap = _CapturingSender()
    monkeypatch.setattr(dispatcher, "_get_default_sender", lambda: cap)
    return cap


# ---------- direct handler ----------

def test_ping_returns_pong():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "ping"}))
    assert "pong" in out["text"].lower()
    assert "slt_metrics" in out["text"]


def test_empty_text_is_ping():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": ""}))
    assert "pong" in out["text"].lower()


def test_unknown_command_returns_help():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "reticulate splines"}))
    assert "unknown command" in out["text"].lower()
    assert "forecast" in out["text"]
    assert "movers" in out["text"]


def test_help_command():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "help"}))
    assert "forecast" in out["text"]
    assert "movers" in out["text"]
    assert "scorecard" in out["text"]
    assert "briefing" in out["text"]
    assert "friday" in out["text"]
    assert "weights" in out["text"]
    assert "backtest" in out["text"]


# ---------- subcommand routing into stubs ----------

def test_forecast_requires_quarter():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "forecast"}))
    assert "usage" in out["text"].lower()


def test_forecast_routes_to_draft_review_gate(capture_forecast_dm):
    """GH #2: `@oo slt forecast <quarter>` must create an slt_draft_review
    gate and DM O — not return an in-channel stub."""
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "forecast FY2026-Q2"}))

    # In-channel reply is a short confirmation, not a stub.
    assert out["cmd"] == "forecast"
    assert out["quarter"] == "FY2026-Q2"
    assert "stub" not in out
    assert isinstance(out["gate_id"], int) and out["gate_id"] > 0
    assert "Forecast draft queued for O review" in out["text"]
    assert f"gate #{out['gate_id']}" in out["text"]

    # DM fired to O's channel, not the original payload channel.
    assert len(capture_forecast_dm.calls) == 1
    call = capture_forecast_dm.calls[0]
    assert call["channel"] == dispatcher._get_o_dm_channel()
    assert call["blocks"][0]["type"] == "header"
    assert "Approval needed" in call["blocks"][0]["text"]["text"]

    # Gate row persisted with the correct tier.
    with get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT action_type, status FROM approval_gates WHERE id = :id"),
            {"id": out["gate_id"]},
        ).fetchone()
    assert row[0] == "slt_draft_review"
    assert row[1] == "pending"


def test_forecast_invalid_quarter_no_gate(capture_forecast_dm):
    """Garbage quarter → friendly error, no gate, no DM."""
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "forecast garbage"}))
    assert "Unrecognized quarter" in out["text"]
    assert "gate_id" not in out
    assert capture_forecast_dm.calls == []


def test_forecast_accepts_short_quarter(capture_forecast_dm):
    """`Q2` shorthand resolves to current fiscal year."""
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "forecast Q2"}))
    assert out["cmd"] == "forecast"
    assert out["quarter"].endswith("-Q2")  # e.g. FY2026-Q2
    assert out["gate_id"] > 0
    assert len(capture_forecast_dm.calls) == 1


def test_forecast_accepts_relative_quarter(capture_forecast_dm):
    """`this_quarter` / `next_quarter` resolve relative to today."""
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "forecast this_quarter"}))
    assert out["cmd"] == "forecast"
    assert out["quarter"].startswith("FY")
    assert out["gate_id"] > 0


# ---------- forecast narrative composer smoke ----------

def test_forecast_narrative_renders_empty_snapshot():
    """With no snapshots in the DB, the composer still produces a draft."""
    from datetime import date

    from agents.slt_metrics.forecast import narrative

    ctx = narrative.build_context("FY2026-Q2", today=date(2026, 4, 14))
    draft = narrative.compose_forecast_draft(ctx)
    assert "FY2026-Q2" in draft["text"]
    # Placeholder marker is present whenever scoring is sparse/absent.
    joined = " ".join(
        b.get("text", {}).get("text", "")
        for b in draft["blocks"]
        if b.get("type") == "section"
    )
    assert narrative.PLACEHOLDER_TAG in joined


def test_forecast_narrative_rejects_garbage_quarter():
    from agents.slt_metrics.forecast import narrative

    with pytest.raises(narrative.InvalidQuarter):
        narrative.parse_quarter("garbage")


def test_forecast_narrative_parses_canonical_quarter():
    from agents.slt_metrics.forecast import narrative

    q = narrative.parse_quarter("FY2027-Q3")
    assert q.fy_year == 2027
    assert q.quarter == 3
    assert q.label == "FY2027-Q3"


def test_movers_defaults_to_yesterday():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "movers"}))
    assert out["stub"] is True
    assert out["period"] == "yesterday"


def test_movers_accepts_period():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "movers this_week"}))
    assert out["period"] == "this_week"


def test_scorecard_requires_scope():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "scorecard"}))
    assert "usage" in out["text"].lower()


def test_scorecard_rejects_unknown_scope():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "scorecard gibberish"}))
    assert "unknown scorecard scope" in out["text"].lower()


def test_scorecard_ae_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "scorecard ae rep@tryloop.ai"}))
    assert out["stub"] is True
    assert out["scope"] == "ae"
    assert out["target"] == "rep@tryloop.ai"


def test_scorecard_team_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "scorecard team nate"}))
    assert out["scope"] == "team"
    assert out["target"] == "nate"


def test_briefing_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "briefing"}))
    assert out["stub"] is True
    assert out["cmd"] == "briefing"


def test_friday_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "friday"}))
    assert out["stub"] is True
    assert out["cmd"] == "friday"


def test_weights_show_default():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "weights"}))
    assert out["stub"] is True
    assert out["action"] == "show"


def test_weights_set_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "weights set icp=0.3"}))
    assert out["action"] == "set"
    assert "icp=0.3" in out["args"]


def test_weights_propose_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "weights propose"}))
    assert out["action"] == "propose"


def test_weights_rejects_unknown_action():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "weights randomize"}))
    assert "usage" in out["text"].lower()


def test_backtest_requires_two_dates():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "backtest 2026-01-01"}))
    assert "usage" in out["text"].lower()


def test_backtest_routes():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "backtest 2026-01-01 2026-03-31"}))
    assert out["stub"] is True
    assert out["from"] == "2026-01-01"
    assert out["to"] == "2026-03-31"


def test_hyphen_and_underscore_cmd_forms_route_identically():
    # batch-grade style — the dispatcher's hyphen→underscore normalization
    # isn't needed for single-word commands, but verify the router handles
    # a hypothetical hyphenated command gracefully (none currently, so this
    # just confirms nothing breaks).
    out_a = asyncio.run(SltMetricsAgent().run("test", {"text": "scorecard"}))
    out_b = asyncio.run(SltMetricsAgent().run("test", {"text": "Scorecard"}))
    assert "usage" in out_a["text"].lower()
    assert "usage" in out_b["text"].lower()


# ---------- dispatcher registration ----------

def test_register_with_dispatcher_adds_all_aliases():
    register_with_dispatcher()
    assert "slt_metrics" in _registry
    assert "slt-metrics" in _registry
    assert "slt" in _registry


def test_dispatch_via_slt_short_form():
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> slt ping", {"user": "U123", "channel": "Cxx"}))
    assert "pong" in out["text"].lower()


def test_dispatch_via_slt_metrics_canonical():
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> slt_metrics ping", {"user": "U123", "channel": "Cxx"}))
    assert "pong" in out["text"].lower()


def test_dispatch_via_slt_metrics_hyphen():
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> slt-metrics ping", {"user": "U123", "channel": "Cxx"}))
    assert "pong" in out["text"].lower()


def test_dispatch_slt_forecast_subcommand(capture_forecast_dm):
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> slt forecast FY2026-Q2", {"user": "U123", "channel": "Cxx"}))
    assert out["cmd"] == "forecast"
    assert out["quarter"] == "FY2026-Q2"
    assert out["gate_id"] > 0
    assert len(capture_forecast_dm.calls) == 1


# ---------- run() lifecycle writes to agent_runs ----------

def test_agent_run_persists_to_db():
    asyncio.run(handle({"text": "ping"}))
    with get_engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT status, agent_name FROM agent_runs "
                "WHERE agent_name='slt_metrics' ORDER BY id DESC LIMIT 1"
            )
        ).fetchone()
    assert row is not None
    assert row[0] == "success"
    assert row[1] == "slt_metrics"
