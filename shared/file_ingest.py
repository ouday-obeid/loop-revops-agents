"""SDR auto-enrichment file ingest — replaces the legacy Zapier flow.

Registered from `shared.slack_dispatcher.build_app` via `@app.event("file_shared")`.
When a CSV/XLSX is dropped in the configured SDR channel, this module downloads
the file, runs the loop-prospecting-engine enrichment pipeline against it via
subprocess, and posts the cleaned XLSX back in-thread.

Env:
  SDR_ENRICHMENT_CHANNEL      required; C... channel ID; handler no-ops if unset
  LOOP_PROSPECTING_ENGINE_PATH optional; defaults to /Users/jarvis/loop-prospecting-engine
  SDR_ENRICHMENT_MAX_MB        optional; defaults to 25
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from slack_sdk.web.async_client import AsyncWebClient

from shared.secrets import get_config, require_secret

log = logging.getLogger(__name__)

SUPPORTED_EXT = {".csv", ".xlsx", ".xls"}
DEFAULT_MAX_MB = 25
SUBPROC_TIMEOUT_SEC = 30 * 60

# Slack fires file_shared multiple times per upload (once on creation, again on
# channel share). Dedup by file_id for a short window so we only run the pipeline
# once per upload.
_DEDUP_TTL_SEC = 600
_seen_file_ids: dict[str, float] = {}
_dedup_lock = asyncio.Lock()


async def _claim_file_id(file_id: str) -> bool:
    """Return True if this invocation should process the file; False if already claimed."""
    async with _dedup_lock:
        now = time.monotonic()
        # Expire stale entries in-line — cheap (few entries) and lock-safe.
        for fid in [k for k, t in _seen_file_ids.items() if now - t > _DEDUP_TTL_SEC]:
            del _seen_file_ids[fid]
        if file_id in _seen_file_ids:
            return False
        _seen_file_ids[file_id] = now
        return True


def is_enabled() -> bool:
    return bool(_target_channel())


def _target_channel() -> str | None:
    chan = get_config("SDR_ENRICHMENT_CHANNEL", "") or ""
    return chan if chan.startswith("C") else None


def _engine_root() -> Path:
    return Path(
        get_config("LOOP_PROSPECTING_ENGINE_PATH", "/Users/jarvis/loop-prospecting-engine")
    )


def _engine_python() -> Path:
    return _engine_root() / "venv" / "bin" / "python"


def _max_bytes() -> int:
    try:
        return int(get_config("SDR_ENRICHMENT_MAX_MB", str(DEFAULT_MAX_MB))) * 1024 * 1024
    except (TypeError, ValueError):
        return DEFAULT_MAX_MB * 1024 * 1024


async def handle_file_shared(client: AsyncWebClient, event: dict[str, Any]) -> None:
    """Event entry point — call with the Slack event payload.

    Slack has already been ACKed upstream in the dispatcher (explicit early
    ack) so this coroutine is free to take minutes without re-delivery.
    """
    target = _target_channel()
    if not target:
        return
    channel_id = event.get("channel_id")
    if channel_id != target:
        return

    file_id = event.get("file_id") or (event.get("file") or {}).get("id")
    if not file_id:
        log.warning("file_shared without file_id; payload=%s", event)
        return

    if not await _claim_file_id(file_id):
        log.info("file_ingest: duplicate file_shared for %s — skipping", file_id)
        return

    try:
        info = await client.files_info(file=file_id)
    except Exception:
        log.exception("files.info failed for %s", file_id)
        return
    file_data = (info.data or {}).get("file", {}) if hasattr(info, "data") else info.get("file", {})
    filename = file_data.get("name", "") or ""
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXT:
        return
    size = int(file_data.get("size") or 0)
    max_bytes = _max_bytes()
    if size > max_bytes:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=event.get("event_ts"),
            text=f":x: `{filename}` is {size / 1024 / 1024:.1f}MB — over the {max_bytes // 1024 // 1024}MB limit.",
        )
        return

    thread_ts = _resolve_thread_ts(file_data, channel_id, event)

    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f":broom: Picked up `{filename}` — dedupe + enrichment starting, ~1–5 min.",
    )

    download_url = file_data.get("url_private_download") or file_data.get("url_private") or ""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, _run_pipeline_sync, download_url, filename, channel_id, thread_ts
    )


def _resolve_thread_ts(file_data: dict[str, Any], channel_id: str, event: dict[str, Any]) -> str:
    shares = file_data.get("shares") or {}
    for bucket in ("public", "private"):
        lst = (shares.get(bucket) or {}).get(channel_id) or []
        if lst:
            ts = lst[0].get("ts")
            if ts:
                return ts
    return event.get("event_ts") or ""


def _run_pipeline_sync(
    download_url: str,
    filename: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    """Blocking path — runs on a worker thread."""
    from slack_sdk import WebClient

    token = require_secret("SLACK_BOT_TOKEN")
    web = WebClient(token=token)

    def say(text: str) -> None:
        try:
            web.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
        except Exception:
            log.exception("chat_postMessage failed")

    if not download_url:
        say(":x: No download URL on the shared file.")
        return

    engine = _engine_root()
    py = _engine_python()
    if not py.exists():
        say(f":x: Pipeline not available: venv missing at `{py}`.")
        return

    try:
        resp = httpx.get(
            download_url,
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
            timeout=60,
        )
    except Exception as e:
        say(f":x: Download failed: {type(e).__name__}: {e}")
        return
    if resp.status_code != 200:
        say(f":x: Download HTTP {resp.status_code}.")
        return

    with tempfile.TemporaryDirectory(prefix="sdr_enrich_") as tmpdir:
        src = Path(tmpdir) / filename
        src.write_bytes(resp.content)

        try:
            proc = subprocess.run(
                [str(py), "main.py", "--test", str(src)],
                cwd=str(engine),
                capture_output=True,
                text=True,
                timeout=SUBPROC_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            say(f":x: Pipeline timed out after {SUBPROC_TIMEOUT_SEC // 60} min.")
            return

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-1500:]
            say(f":x: Pipeline failed (rc={proc.returncode}):\n```{tail}```")
            return

        result = engine / "data" / f"{Path(filename).stem}_cleaned.xlsx"
        if not result.exists():
            tail = (proc.stdout or "")[-800:]
            say(f":x: Output not produced. Stdout tail:\n```{tail}```")
            return

        try:
            web.files_upload_v2(
                channel=channel_id,
                thread_ts=thread_ts,
                file=str(result),
                filename=result.name,
                initial_comment=f":white_check_mark: Dedup + enrich complete for `{filename}`",
            )
        except Exception as e:
            say(f":x: Upload failed: {type(e).__name__}: {e}")
