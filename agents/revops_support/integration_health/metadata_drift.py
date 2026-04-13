"""Detect SF metadata drift vs the most recent weekly snapshot.

A weekly snapshot lives at `var/knowledge_snapshots/<YYYY-MM-DD>/sf_object_model.md`
(produced by knowledge_refresh.metadata_snapshotter). This monitor re-describes
each watched SObject, reduces it to a comparable `(field, type)` signature, and
diffs against the signature it built last run (stored at
`var/knowledge_snapshots/metadata_drift.json`).

First call with no prior state records the current signatures and returns zero
drift — drift only registers once we have a baseline to compare against.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agents.revops_support.knowledge_refresh import metadata_snapshotter
from shared.mcp import salesforce_mcp

from ._task_surface import surface_task

log = logging.getLogger(__name__)

WATCHED_OBJECTS: tuple[str, ...] = (
    "Account",
    "Contact",
    "Opportunity",
    "Lead",
    "User",
)
TASK_CATEGORY = "sf_integration_health"


@dataclass
class DriftRow:
    sobject: str
    added: list[str]
    removed: list[str]
    changed: list[str]  # "FieldName: old_type→new_type"

    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


def _state_path() -> Path:
    return metadata_snapshotter._snapshots_root() / "metadata_drift.json"


def _signature_from_describe(describe: dict[str, Any]) -> dict[str, str]:
    """Reduce an sObject describe to {field_name: field_type}."""
    out: dict[str, str] = {}
    for f in describe.get("fields") or []:
        name = f.get("name")
        if not name:
            continue
        out[name] = f.get("type") or "unknown"
    return out


def diff_signature(
    old: dict[str, str], new: dict[str, str], sobject: str,
) -> DriftRow:
    old_keys = set(old)
    new_keys = set(new)
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    changed = sorted(
        f"{k}: {old[k]}→{new[k]}" for k in old_keys & new_keys if old[k] != new[k]
    )
    return DriftRow(sobject=sobject, added=added, removed=removed, changed=changed)


def _load_prior() -> dict[str, dict[str, str]]:
    p = _state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        log.warning("metadata_drift state file corrupt; starting fresh")
        return {}


def _save_current(state: dict[str, dict[str, str]]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, sort_keys=True, indent=2))


def poll(
    *,
    sobjects: tuple[str, ...] = WATCHED_OBJECTS,
    describe_fn=None,
) -> list[DriftRow]:
    describe = describe_fn or salesforce_mcp.describe_sobject
    prior = _load_prior()
    current: dict[str, dict[str, str]] = {}
    drift_rows: list[DriftRow] = []

    for s in sobjects:
        try:
            desc = describe(s)
        except Exception as e:  # noqa: BLE001
            log.warning("metadata_drift: describe failed for %s: %s", s, e)
            continue
        sig = _signature_from_describe(desc)
        current[s] = sig

        if s in prior:
            row = diff_signature(prior[s], sig, s)
            if not row.is_empty():
                drift_rows.append(row)

    _save_current(current)

    for row in drift_rows:
        source = f"revops_support:metadata_drift:{row.sobject}"
        title = (
            f"SF {row.sobject} metadata drifted "
            f"(+{len(row.added)} / -{len(row.removed)} / ~{len(row.changed)})"
        )
        description = (
            f"Detected by revops_support metadata_drift. "
            f"Added: {row.added[:10]}; removed: {row.removed[:10]}; "
            f"changed: {row.changed[:10]}. "
            "Review whether knowledge/canonical docs need to be refreshed."
        )
        surface_task(
            source=source,
            title=title,
            description=description,
            category=TASK_CATEGORY,
            priority="medium",
            metadata={
                "sobject": row.sobject,
                "added": row.added,
                "removed": row.removed,
                "changed": row.changed,
            },
        )

    log.info("metadata_drift: drift rows=%d (prior empty=%s)", len(drift_rows), not prior)
    return drift_rows
