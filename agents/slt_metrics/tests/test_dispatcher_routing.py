"""Dispatcher routing tests — ping, unknown, help, each subcommand stub, aliases."""
from __future__ import annotations

import asyncio

from sqlalchemy import text

from agents.slt_metrics.agent import SltMetricsAgent
from agents.slt_metrics.main import handle, register_with_dispatcher
from shared.db.connection import get_engine
from shared.slack_dispatcher import _registry, dispatch


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


def test_forecast_routes_to_stub():
    out = asyncio.run(SltMetricsAgent().run("test", {"text": "forecast FY2026-Q2"}))
    assert out["stub"] is True
    assert out["cmd"] == "forecast"
    assert out["quarter"] == "FY2026-Q2"


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


def test_dispatch_slt_forecast_subcommand():
    register_with_dispatcher()
    out = asyncio.run(dispatch("<@BOTID> slt forecast FY2026-Q2", {"user": "U123", "channel": "Cxx"}))
    assert out["stub"] is True
    assert out["cmd"] == "forecast"


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
