"""D1 smoke tests — scaffolding compiles + ping returns pong + dispatcher wiring.

Real capability tests ship D2+. These exist so `pytest agents/top_of_funnel/tests`
passes from the moment the agent directory is created, which lets CI gate every
subsequent commit on a green baseline.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_handler_ping_returns_pong():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "ping", "user_id": "U_TEST"})
    assert "pong" in result["text"].lower()


@pytest.mark.asyncio
async def test_handler_empty_text_returns_pong():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "", "user_id": "U_TEST"})
    assert "pong" in result["text"].lower()


@pytest.mark.asyncio
async def test_handler_unknown_command_returns_help():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "banana", "user_id": "U_TEST"})
    assert "unknown command" in result["text"].lower()
    assert "enrich" in result["text"]
    assert "score" in result["text"]


@pytest.mark.asyncio
async def test_handler_help_command():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "help", "user_id": "U_TEST"})
    assert "enrich" in result["text"]
    assert "suppress" in result["text"]
    assert "queue" in result["text"]


@pytest.mark.asyncio
async def test_enrich_without_args_returns_usage():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "enrich", "user_id": "U_TEST"})
    assert "Usage" in result["text"]
    assert "domain" in result["text"].lower()


@pytest.mark.asyncio
async def test_score_without_args_returns_usage():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "score", "user_id": "U_TEST"})
    assert "Usage" in result["text"]


@pytest.mark.asyncio
async def test_suppress_without_args_returns_usage():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "suppress", "user_id": "U_TEST"})
    assert "Usage" in result["text"]
    assert "email" in result["text"].lower()


@pytest.mark.asyncio
async def test_queue_approve_requires_numeric_gate_id():
    from agents.top_of_funnel.main import handle

    result = await handle({"text": "queue approve not_a_number", "user_id": "U_TEST"})
    assert "Usage" in result["text"]


def test_state_sql_parses():
    """state.sql applies cleanly to the agent SQLite DB (applied by conftest)."""
    from agents.top_of_funnel.state import get_state_engine
    from sqlalchemy import text

    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    tables = {r[0] for r in rows}
    expected = {
        "clay_credit_ledger",
        "suppression_cache",
        "tof_enrichment_runs",
        "tof_lead_candidates",
        "tof_routing_state",
        "apollo_query_cache",
    }
    assert expected.issubset(tables), f"missing tables: {expected - tables}"


def test_configs_load():
    """All three YAMLs parse and have the expected top-level keys."""
    from pathlib import Path

    import yaml

    cfg_dir = Path(__file__).parent.parent / "config"

    weights = yaml.safe_load((cfg_dir / "icp_weights.yaml").read_text())
    assert "dimensions" in weights or "ownership" in weights or any(
        k in weights for k in ("ownership_weight", "weights", "scoring")
    ), f"icp_weights.yaml missing scoring keys: {list(weights)}"

    icp_config = yaml.safe_load((cfg_dir / "icp_config.yaml").read_text())
    assert icp_config, "icp_config.yaml empty"

    territory = yaml.safe_load((cfg_dir / "territory.yaml").read_text())
    assert any(k in territory for k in ("ENT", "enterprise", "segments")), (
        f"territory.yaml missing segments: {list(territory)}"
    )


def test_register_with_dispatcher_exposes_both_aliases(monkeypatch):
    """main.register_with_dispatcher should register both 'top_of_funnel' and 'tof'."""
    registered: dict[str, object] = {}

    def fake_register(name, handler):
        registered[name] = handler

    import agents.top_of_funnel.main as tof_main

    monkeypatch.setattr(tof_main, "register", fake_register)
    tof_main.register_with_dispatcher()

    assert "top_of_funnel" in registered
    assert "tof" in registered
    assert registered["top_of_funnel"] is registered["tof"]
