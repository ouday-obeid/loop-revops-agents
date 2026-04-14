"""Scheduler entrypoints for the knowledge refresh pipeline.

- `run_weekly_snapshot()`  → Sunday 02:00 cron: write metadata snapshot.
- `send_weekly_digest()`   → Monday 09:00 cron: diff snapshot vs canonical,
                              DM O the summary.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from agents.revops_support.knowledge_refresh import diff_producer, metadata_snapshotter
from shared.secrets import get_config

log = logging.getLogger(__name__)

_DIGEST_CHANNEL_ENV = "REVOPS_KNOWLEDGE_DIGEST_CHANNEL"
_DIGEST_USER_ENV = "REVOPS_KNOWLEDGE_DIGEST_USER"


def run_weekly_snapshot() -> dict[str, Path]:
    """Write today's snapshot under var/knowledge_snapshots/<YYYY-MM-DD>/."""
    log.info("weekly snapshot start")
    paths = metadata_snapshotter.snapshot()
    log.info("weekly snapshot complete: %s", {k: str(v) for k, v in paths.items()})
    return paths


def _find_most_recent_snapshot_dir() -> Path | None:
    """Pick the newest date-named subdir under the snapshots root."""
    root = metadata_snapshotter._snapshots_root()
    if not root.exists():
        return None
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.name)


def send_weekly_digest() -> dict[str, object]:
    """Build a diff summary vs canonical and DM it to O (channel configurable)."""
    snap_dir = _find_most_recent_snapshot_dir()
    if snap_dir is None:
        log.warning("no snapshot dir found under %s; skipping digest", metadata_snapshotter._snapshots_root())
        return {"status": "no_snapshot", "snapshot_dir": None, "message_ts": None}

    # If the snapshot is > 10 days stale, flag it but still try to send.
    try:
        snap_date = date.fromisoformat(snap_dir.name)
        age_days = (date.today() - snap_date).days
        if age_days > 10:
            log.warning("latest snapshot is %d days old: %s", age_days, snap_dir)
    except ValueError:
        age_days = None

    canon = diff_producer.canonical_dir()
    summary = diff_producer.render_summary(
        snap_dir, canon, metadata_snapshotter.SNAPSHOT_FILES
    )

    channel = get_config(_DIGEST_CHANNEL_ENV) or "oo-dm"
    user = get_config(_DIGEST_USER_ENV)

    # Import inside the function so tests can monkeypatch SlackSender cleanly.
    from shared.slack_dispatcher import SlackSender

    sender = SlackSender()
    send_kwargs = {"text_": summary[:2800]}  # Slack blocks cap at ~3k chars
    ts = sender.send(channel=user or channel, **send_kwargs)
    log.info("sent weekly knowledge digest to %s (ts=%s)", user or channel, ts)
    return {
        "status": "sent",
        "snapshot_dir": str(snap_dir),
        "age_days": age_days,
        "channel": user or channel,
        "message_ts": ts,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snapshot", "digest"])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.cmd == "snapshot":
        print(run_weekly_snapshot())
    else:
        print(send_weekly_digest())
