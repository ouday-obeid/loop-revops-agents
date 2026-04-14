"""Command router tests — phrase matching and help/ping surface."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agents.revops_support.agent import RevOpsSupportAgent


@pytest.mark.asyncio
async def test_ping_returns_pong():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "ping"})
    assert "pong" in result["text"].lower()


@pytest.mark.asyncio
async def test_help_returns_command_list():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "help"})
    assert "pipeline by stage" in result["text"]
    assert "validation rules" in result["text"]


@pytest.mark.asyncio
async def test_help_long_flag_routes_to_help():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "--help"})
    assert "pipeline by stage" in result["text"]
    assert "Unknown" not in result["text"]


@pytest.mark.asyncio
async def test_help_short_flag_routes_to_help():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "-h"})
    assert "pipeline by stage" in result["text"]
    assert "Unknown" not in result["text"]


@pytest.mark.asyncio
async def test_pipeline_by_stage_routes_to_canned():
    agent = RevOpsSupportAgent()
    with patch("agents.revops_support.agent.canned.pipeline_by_stage") as mock_fn:
        mock_fn.return_value = {"records": [], "text": "ok", "blocks": []}
        result = await agent.handle("slack", {"text": "pipeline by stage"})
    mock_fn.assert_called_once()
    assert result["text"] == "ok"


@pytest.mark.asyncio
async def test_stale_opportunities_extracts_days():
    agent = RevOpsSupportAgent()
    with patch("agents.revops_support.agent.canned.stale_opportunities") as mock_fn:
        mock_fn.return_value = {"records": [], "text": "", "blocks": []}
        await agent.handle("slack", {"text": "stale opportunities 60"})
    mock_fn.assert_called_once_with(60)


@pytest.mark.asyncio
async def test_validation_rules_requires_object_name():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "validation rules"})
    assert "Usage" in result["text"]


@pytest.mark.asyncio
async def test_validation_rules_extracts_object():
    agent = RevOpsSupportAgent()
    with patch("agents.revops_support.agent.canned.validation_rule_violations") as mock_fn:
        mock_fn.return_value = {"records": [], "text": "", "blocks": []}
        await agent.handle("slack", {"text": "validation rules Opportunity"})
    mock_fn.assert_called_once_with("Opportunity")


@pytest.mark.asyncio
async def test_unknown_command_shows_help():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "do the thing"})
    assert "Unknown revops-support command" in result["text"]


@pytest.mark.asyncio
async def test_future_commands_return_not_wired():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "schema create field"})
    assert "later in Phase 1" in result["text"]


@pytest.mark.asyncio
async def test_soql_passthrough():
    agent = RevOpsSupportAgent()
    fake = {"records": [{"Id": "001X"}], "totalSize": 1, "done": True}
    with patch("agents.revops_support.agent.soql_engine.run", return_value=fake):
        result = await agent.handle("slack", {"text": "soql SELECT Id FROM Account"})
    assert "1 rows" in result["text"]


@pytest.mark.asyncio
async def test_soql_rejects_empty():
    agent = RevOpsSupportAgent()
    result = await agent.handle("slack", {"text": "soql "})
    assert "Usage" in result["text"]
