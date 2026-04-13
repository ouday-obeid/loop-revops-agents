"""ForecastWeights persistence — reads/writes via `forecast_history.metadata`.

The weights themselves are just the dataclass in `agents.slt_metrics.types`.
This module handles:

  - get_current_weights(): latest active version (falls back to WEIGHT_SEEDS)
  - save_weights(weights, justification, approval_gate_id): write a new
    forecast_history row marking the new active version
  - list_versions(): audit trail for O's weights history

We piggyback on forecast_history instead of adding a `weights` table: the
row already carries `weights_version` + `metadata`, every weight change is
naturally tied to a run_date, and the backtest script can replay any prior
version without a join.

**Not approval-enforcing**: callers (dispatcher's `weights set` subcommand)
must pass an approved gate id to `save_weights`. Enforcement lives at the
governance layer so the same write path works for O's explicit adjustments
and the weight tuner's top-3 proposals.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, replace
from datetime import date
from typing import Any

from sqlalchemy import text

from shared.db.connection import get_engine

from agents.slt_metrics.pipeline.config import WEIGHT_SEEDS
from agents.slt_metrics.types import ForecastWeights

log = logging.getLogger(__name__)

# forecast_history.metadata key — lets us distinguish weight-update rows from
# regular forecast runs. Regular runs also write weights inline for replay,
# but only `_WEIGHTS_ROW_KIND` rows are canonical activation points.
_WEIGHTS_ROW_KIND = "weights_update"


def get_current_weights() -> ForecastWeights:
    """Return the most recent active ForecastWeights.

    Falls back to WEIGHT_SEEDS when the table is empty or the latest row has
    no parseable weights blob — a brand-new deploy should score with the
    seed, not crash.
    """
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                SELECT weights_version, metadata
                  FROM forecast_history
                 WHERE metadata LIKE :pattern
                 ORDER BY run_date DESC, id DESC
                 LIMIT 1
                """
            ),
            {"pattern": f'%"{_WEIGHTS_ROW_KIND}"%'},
        ).fetchone()

    if row is None:
        return WEIGHT_SEEDS

    try:
        blob = json.loads(row[1]) if row[1] else {}
    except json.JSONDecodeError:
        log.warning("get_current_weights: unparseable metadata for version=%s", row[0])
        return WEIGHT_SEEDS

    weights_blob = blob.get("weights") or {}
    if not weights_blob:
        return WEIGHT_SEEDS

    try:
        return ForecastWeights(
            icp=float(weights_blob["icp"]),
            stage=float(weights_blob["stage"]),
            activity=float(weights_blob["activity"]),
            timeline=float(weights_blob["timeline"]),
            call=float(weights_blob["call"]),
            version=str(row[0] or weights_blob.get("version") or WEIGHT_SEEDS.version),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("get_current_weights: incomplete weights blob; falling back")
        return WEIGHT_SEEDS


def save_weights(
    weights: ForecastWeights,
    *,
    justification: str,
    approval_gate_id: int | None = None,
    horizon_quarter: str = "FY-CURRENT",
    run_date: date | None = None,
) -> int:
    """Persist a new weights version. Returns the forecast_history row id.

    `approval_gate_id` must already be approved (caller enforces via
    `require_approved_gate`). We record the gate id on the row so the audit
    trail ties the change to a specific O decision.
    """
    if not _weights_sum_to_one(weights):
        raise ValueError(
            f"weights do not sum to 1.0 "
            f"(icp={weights.icp} stage={weights.stage} "
            f"activity={weights.activity} timeline={weights.timeline} call={weights.call})"
        )

    run_date = run_date or date.today()
    metadata = {
        "kind": _WEIGHTS_ROW_KIND,
        "weights": _weights_dict(weights),
        "justification": justification,
        "approval_gate_id": approval_gate_id,
    }

    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                INSERT INTO forecast_history
                    (run_date, horizon_quarter, weights_version,
                     commit_amount, best_case_amount, weighted_amount,
                     deal_count, metadata)
                VALUES
                    (:run_date, :horizon, :version, 0, 0, 0, 0, :metadata)
                ON CONFLICT (run_date, horizon_quarter, weights_version) DO NOTHING
                """
            ),
            {
                "run_date": run_date,
                "horizon": horizon_quarter,
                "version": weights.version,
                "metadata": json.dumps(metadata),
            },
        )
        row_id = result.lastrowid
        if row_id is None or row_id == 0:
            # ON CONFLICT DO NOTHING path — look up the existing row.
            existing = conn.execute(
                text(
                    "SELECT id FROM forecast_history "
                    "WHERE run_date = :r AND horizon_quarter = :h AND weights_version = :v"
                ),
                {"r": run_date, "h": horizon_quarter, "v": weights.version},
            ).scalar()
            row_id = int(existing) if existing else 0
    log.info(
        "save_weights: version=%s icp=%.2f stage=%.2f activity=%.2f timeline=%.2f call=%.2f",
        weights.version, weights.icp, weights.stage, weights.activity,
        weights.timeline, weights.call,
    )
    return int(row_id or 0)


def list_versions(limit: int = 20) -> list[dict[str, Any]]:
    """Audit trail: recent weight-update rows with their metadata."""
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, run_date, weights_version, metadata
                  FROM forecast_history
                 WHERE metadata LIKE :pattern
                 ORDER BY run_date DESC, id DESC
                 LIMIT :lim
                """
            ),
            {"pattern": f'%"{_WEIGHTS_ROW_KIND}"%', "lim": limit},
        ).mappings().all()
    out: list[dict[str, Any]] = []
    for row in rows:
        blob = json.loads(row["metadata"]) if row["metadata"] else {}
        out.append({
            "id": row["id"],
            "run_date": row["run_date"],
            "version": row["weights_version"],
            "weights": blob.get("weights"),
            "justification": blob.get("justification"),
            "approval_gate_id": blob.get("approval_gate_id"),
        })
    return out


def bump_version(weights: ForecastWeights, label: str) -> ForecastWeights:
    """Return a copy of `weights` with a fresh version string.

    Convention: `v{N}-{label}-YYYY-MM-DD`. Callers typically use the label
    'tuned', 'o-manual', or 'rollback'.
    """
    today = date.today().isoformat()
    prev = weights.version
    # Strip existing v{N} prefix if present; otherwise start at v2 (seed = v1).
    n = _next_version_n(prev)
    return replace(weights, version=f"v{n}-{label}-{today}")


# ------------------------------------------------------------------ helpers

def _weights_sum_to_one(w: ForecastWeights, tolerance: float = 1e-6) -> bool:
    total = w.icp + w.stage + w.activity + w.timeline + w.call
    return abs(total - 1.0) <= tolerance


def _weights_dict(w: ForecastWeights) -> dict[str, Any]:
    # asdict() yields the same shape but we pin it here so schema drift on
    # ForecastWeights doesn't silently change the on-disk blob.
    d = asdict(w)
    return {
        "icp": d["icp"], "stage": d["stage"], "activity": d["activity"],
        "timeline": d["timeline"], "call": d["call"], "version": d["version"],
    }


def _next_version_n(previous_version: str) -> int:
    if previous_version.startswith("v") and "-" in previous_version:
        prefix = previous_version.split("-", 1)[0][1:]
        try:
            return int(prefix) + 1
        except ValueError:
            return 2
    return 2
