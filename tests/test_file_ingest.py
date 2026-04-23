"""Unit tests for shared.file_ingest — SDR auto-enrichment handler guards.

Subprocess + network paths are covered by smoke tests, not unit tests.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clear_dedup():
    from shared import file_ingest as _fi

    _fi._seen_file_ids.clear()
    yield
    _fi._seen_file_ids.clear()


@pytest.fixture
def disabled_env(monkeypatch):
    monkeypatch.delenv("SDR_ENRICHMENT_CHANNEL", raising=False)


@pytest.fixture
def enabled_env(monkeypatch):
    monkeypatch.setenv("SDR_ENRICHMENT_CHANNEL", "C_SDR")


def test_is_enabled_false_without_env(disabled_env):
    from shared import file_ingest

    assert file_ingest.is_enabled() is False


def test_is_enabled_false_with_non_channel_id(monkeypatch):
    monkeypatch.setenv("SDR_ENRICHMENT_CHANNEL", "lead-drops")
    from shared import file_ingest

    assert file_ingest.is_enabled() is False


def test_is_enabled_true_when_channel_id(enabled_env):
    from shared import file_ingest

    assert file_ingest.is_enabled() is True


async def test_handler_noop_when_disabled(disabled_env):
    from shared.file_ingest import handle_file_shared

    client = MagicMock()
    client.files_info = AsyncMock()
    client.chat_postMessage = AsyncMock()

    await handle_file_shared(client, {"channel_id": "C_SDR", "file_id": "F1"})

    client.files_info.assert_not_called()
    client.chat_postMessage.assert_not_called()


async def test_handler_noop_on_wrong_channel(enabled_env):
    from shared.file_ingest import handle_file_shared

    client = MagicMock()
    client.files_info = AsyncMock()
    client.chat_postMessage = AsyncMock()

    await handle_file_shared(client, {"channel_id": "C_OTHER", "file_id": "F1"})

    client.files_info.assert_not_called()
    client.chat_postMessage.assert_not_called()


async def test_handler_noop_on_unsupported_extension(enabled_env):
    from shared.file_ingest import handle_file_shared

    client = MagicMock()
    client.files_info = AsyncMock(
        return_value=SimpleNamespace(data={"file": {"name": "report.pdf", "size": 1024}})
    )
    client.chat_postMessage = AsyncMock()

    await handle_file_shared(client, {"channel_id": "C_SDR", "file_id": "F1"})

    client.files_info.assert_awaited_once()
    client.chat_postMessage.assert_not_called()


async def test_handler_dedups_duplicate_file_shared(enabled_env):
    # Slack fires file_shared multiple times per upload; make sure we only
    # process each file_id once.
    from shared.file_ingest import handle_file_shared

    client = MagicMock()
    client.files_info = AsyncMock(
        return_value=SimpleNamespace(data={"file": {"name": "report.pdf", "size": 1024}})
    )
    client.chat_postMessage = AsyncMock()

    # Extension filter will skip these, but the claim happens earlier so the
    # second call should not reach files_info.
    await handle_file_shared(client, {"channel_id": "C_SDR", "file_id": "FDUP"})
    await handle_file_shared(client, {"channel_id": "C_SDR", "file_id": "FDUP"})

    assert client.files_info.await_count == 1


async def test_handler_rejects_oversize(enabled_env, monkeypatch):
    monkeypatch.setenv("SDR_ENRICHMENT_MAX_MB", "1")
    from shared import file_ingest
    from shared.file_ingest import handle_file_shared

    assert file_ingest._max_bytes() == 1024 * 1024

    client = MagicMock()
    client.files_info = AsyncMock(
        return_value=SimpleNamespace(
            data={"file": {"name": "big.csv", "size": 5 * 1024 * 1024}}
        )
    )
    client.chat_postMessage = AsyncMock()

    await handle_file_shared(client, {"channel_id": "C_SDR", "file_id": "F1"})

    client.files_info.assert_awaited_once()
    client.chat_postMessage.assert_awaited_once()
    call_args = client.chat_postMessage.await_args
    assert "over the" in call_args.kwargs["text"]
