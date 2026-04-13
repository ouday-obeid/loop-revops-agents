"""Verify `@oo revops-support <cmd>` routes to the real handler, not OO's stub."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.revops_support.main import register_with_dispatcher
from shared import slack_dispatcher


@pytest.fixture(autouse=True)
def _reset_registry():
    # Save + restore the registry so tests don't leak handlers across sessions.
    saved = dict(slack_dispatcher._registry)
    slack_dispatcher._registry.clear()
    yield
    slack_dispatcher._registry.clear()
    slack_dispatcher._registry.update(saved)


@pytest.mark.asyncio
async def test_underscore_and_hyphen_both_route():
    register_with_dispatcher()
    assert "revops_support" in slack_dispatcher._registry
    assert "revops-support" in slack_dispatcher._registry


@pytest.mark.asyncio
async def test_pipeline_by_stage_dispatched_to_real_handler():
    register_with_dispatcher()
    with patch("agents.revops_support.agent.canned.pipeline_by_stage") as mock_fn:
        mock_fn.return_value = {"records": [], "text": "PIPELINE", "blocks": []}
        result = await slack_dispatcher.dispatch(
            "<@U0BOT> oo revops-support pipeline by stage",
            context={"user": "U07P4GX9YLQ", "channel": "C_TEST"},
        )
    mock_fn.assert_called_once()
    assert result["text"] == "PIPELINE"


@pytest.mark.asyncio
async def test_unregistered_specialist_still_routed_to_oo():
    # Only oo + revops_support registered — sales_reps routing goes to oo's stub.
    from agents.oo import main as oo_main
    register_with_dispatcher()
    slack_dispatcher.register("oo", oo_main.oo_dispatcher.handle)
    result = await slack_dispatcher.dispatch(
        "<@U0BOT> oo sales_reps some_cmd",
        context={"user": "U07P4GX9YLQ", "channel": "C_TEST"},
    )
    # OO's current fallback for unknown commands returns a "no handler wired" note.
    assert "received" in result["text"].lower() or "handler" in result["text"].lower() or "sales_reps" in result["text"].lower()
