"""Fire canary verify() checks at their scheduled T+30m/2h/4h intervals.

Reads every `pending_changes/*/change.yaml` marked `canary: true` and compares
`scheduled_verifications[*].at` to now. For each elapsed entry that hasn't
been verified yet, invokes `first_task_ceo_tier.verify()` and — on drift —
pages O via Slack. The verify() call stamps the manifest itself, so we use
presence of a matching `verifications[*].interval_min` entry as the idempotency
marker.

Runs every 15 min; 15m < shortest interval (30m), so no schedules slip.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import yaml

from .change_proposer import _pending_dir
from .first_task_ceo_tier import (
    CanaryPlan,
    Snapshot,
    verify,
)

log = logging.getLogger(__name__)

AGENT_NAME = "revops_support"


def _load_canary_manifests() -> list[tuple[str, dict[str, Any]]]:
    root = _pending_dir()
    if not root.exists():
        return []
    manifests: list[tuple[str, dict[str, Any]]] = []
    for bundle in sorted(root.iterdir()):
        mf = bundle / "change.yaml"
        if not mf.exists():
            continue
        data = yaml.safe_load(mf.read_text()) or {}
        if data.get("canary") and data.get("status") == "deployed":
            manifests.append((bundle.name, data))
    return manifests


def _already_verified(manifest: dict[str, Any], interval_min: int) -> bool:
    return any(
        v.get("interval_min") == interval_min
        for v in manifest.get("verifications", [])
    )


def _drift_alert(
    slug: str, interval_min: int, drift: dict[str, Any],
    *, slack_sender_cls=None,
) -> str | None:
    if slack_sender_cls is None:
        from shared.slack_dispatcher import SlackSender as slack_sender_cls  # noqa: N813
    text_body = (
        f":rotating_light: *Canary drift* slug=`{slug}` at T+{interval_min}m\n"
        f"Role membership changed post-deploy: {drift}\n"
        f"Consider `rollback.prepare('{slug}', justification=...)`."
    )
    sender = slack_sender_cls()
    return sender.send(channel="oo-dm", text_=text_body)


def poll(
    *,
    sf_mcp: Any | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Scan canary bundles; fire due verifications. Returns per-check results."""
    now = now or datetime.now(timezone.utc)
    if sf_mcp is None:
        from shared.mcp import salesforce_mcp as _sf
        sf_mcp = _sf

    results: list[dict[str, Any]] = []
    for slug, manifest in _load_canary_manifests():
        scheduled = manifest.get("scheduled_verifications") or []
        pre_data = manifest.get("pre_snapshot") or {}
        if not pre_data:
            log.warning("canary %s has no pre_snapshot; skipping", slug)
            continue
        pre = Snapshot(
            taken_at=pre_data.get("taken_at", ""),
            counts_by_role=pre_data.get("counts_by_role", {}),
        )
        plan = CanaryPlan(
            slug=slug,
            path=_pending_dir() / slug,
            gate_id=manifest.get("approval_gate_id", 0),
        )
        for sched in scheduled:
            try:
                due = datetime.fromisoformat(sched["at"])
            except (KeyError, ValueError):
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            if due > now:
                continue
            interval_min = int(sched.get("interval_min", 0))
            if _already_verified(manifest, interval_min):
                continue

            result = verify(
                plan, pre, interval_min=interval_min, sf_mcp=sf_mcp, now=now,
            )
            out: dict[str, Any] = {
                "slug": slug,
                "interval_min": interval_min,
                "passed": result.passed,
            }
            if not result.passed:
                try:
                    out["slack_ts"] = _drift_alert(slug, interval_min, result.drift)
                except Exception as e:  # noqa: BLE001
                    log.error("drift alert failed slug=%s: %s", slug, e)
                    out["slack_error"] = str(e)[:200]
            results.append(out)

    log.info("canary poll complete: %d check(s)", len(results))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for row in poll():
        print(row)
