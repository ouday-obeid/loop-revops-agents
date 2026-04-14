"""Replay historical opp states through the scorer and grade accuracy.

The model is only trustworthy once we can answer "what did the 5-pillar scorer
predict last Monday for the deals that since closed?". This module takes the
current record of each opportunity plus its change log (OpportunityHistory +
OpportunityFieldHistory, pulled by the caller) and rolls each opp back to its
state on each prior week, scores it, and grades the prediction against the
eventual outcome.

Primary metric is weighted-ACV MAPE per weekly cohort. Brier score on
`P(close | score ≥ 60)` + per-category hit-rate are surfaced alongside so the
weight tuner (D10) has more than one signal to optimize against.

This module is intentionally pure — the caller is responsible for pulling the
change logs (SOQL is I/O-heavy and belongs in `pipeline/fetcher.py`). Keeps
the replay testable with synthetic change streams.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

from agents.slt_metrics.forecast.scorer import score_deal
from agents.slt_metrics.pipeline.config import (
    BEST_CASE_SCORE_THRESHOLD,
    COMMIT_SCORE_THRESHOLD,
    PROBABILITY_BANDS,
)
from agents.slt_metrics.scorecards.quota import current_quarter_label
from agents.slt_metrics.types import ForecastWeights, OppRecord
from shared.db.connection import get_engine

log = logging.getLogger(__name__)


# ------------------------------------------------------------------ change log

@dataclass(frozen=True)
class StageChange:
    """One row of SF's OpportunityHistory for stage transitions."""
    opp_id: str
    changed_at: datetime
    from_stage: str | None
    to_stage: str


@dataclass(frozen=True)
class FieldChange:
    """One row of SF's OpportunityFieldHistory.

    `field` is the SF API name (e.g. `CloseDate`, `ACV__c`, `Amount`,
    `OwnerId`). `old_value` / `new_value` are whatever SF serialized — the
    rollback logic coerces per-field.
    """
    opp_id: str
    changed_at: datetime
    field: str
    old_value: Any
    new_value: Any


# ------------------------------------------------------------------ result

@dataclass
class CohortMetric:
    cohort_week: date
    deal_count: int
    weighted_acv: float
    actuals_at_close: float
    mape: float | None


@dataclass
class BacktestResult:
    weights_version: str
    window_start: date
    window_end: date
    step_days: int
    cohorts: list[CohortMetric]
    overall_mape: float | None
    brier_score: float | None
    category_hit_rate: dict[str, float]
    deal_count: int
    actuals_total: float
    weighted_total: float
    commit_total: float
    best_case_total: float

    def as_metadata(self) -> dict[str, Any]:
        return {
            "weights_version": self.weights_version,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "step_days": self.step_days,
            "overall_mape": self.overall_mape,
            "brier_score": self.brier_score,
            "category_hit_rate": self.category_hit_rate,
            "deal_count": self.deal_count,
            "actuals_total": self.actuals_total,
            "weighted_total": self.weighted_total,
            "commit_total": self.commit_total,
            "best_case_total": self.best_case_total,
            "cohorts": [
                {
                    "week": c.cohort_week.isoformat(),
                    "deal_count": c.deal_count,
                    "weighted_acv": c.weighted_acv,
                    "actuals_at_close": c.actuals_at_close,
                    "mape": c.mape,
                }
                for c in self.cohorts
            ],
        }


# ------------------------------------------------------------------ public API

def backtest(
    *,
    base_opps: Iterable[OppRecord],
    stage_changes: Iterable[StageChange],
    field_changes: Iterable[FieldChange],
    weights: ForecastWeights,
    window_start: date,
    window_end: date,
    step_days: int = 7,
) -> BacktestResult:
    """Replay historical state through the scorer + compute accuracy metrics.

    `base_opps` are the CURRENT state rows (we rewind). Expects `is_closed` /
    `is_won` to reflect eventual outcome — the grader uses those as ground
    truth when the opp closed inside the window.

    `stage_changes` + `field_changes` come from SOQL — the caller pulls them
    from OpportunityHistory + OpportunityFieldHistory. Ordering is imposed
    here (no assumption on caller-side sort).
    """
    if step_days <= 0:
        raise ValueError("step_days must be positive")
    if window_end < window_start:
        raise ValueError("window_end must be >= window_start")

    base_by_id = {o.id: o for o in base_opps}
    stage_by_opp = _group(stage_changes, key=lambda s: s.opp_id)
    field_by_opp = _group(field_changes, key=lambda f: f.opp_id)

    weekly_cohorts: dict[date, list[tuple[OppRecord, float, int]]] = {}
    # (base_opp, weighted_acv, score) per cohort week — base_opp holds the
    # eventual outcome so we can grade predictions against actuals.

    brier_pairs: list[tuple[float, int]] = []  # (probability, outcome 0/1)
    category_outcomes: dict[str, list[int]] = {}  # category -> [outcomes]
    deal_count = 0
    weighted_total = 0.0
    commit_total = 0.0
    best_case_total = 0.0

    for as_of in _week_steps(window_start, window_end, step_days):
        cohort = weekly_cohorts.setdefault(as_of, [])
        for opp in base_by_id.values():
            rolled = _rewind_opp(
                opp,
                stage_changes=stage_by_opp.get(opp.id, ()),
                field_changes=field_by_opp.get(opp.id, ()),
                as_of=as_of,
            )
            if rolled is None:
                continue
            if rolled.is_closed and rolled.close_date is not None and rolled.close_date <= as_of:
                continue  # Deal already closed by this cohort — don't re-predict.
            scored = score_deal(rolled, weights, today=as_of)
            cohort.append((opp, scored.weighted_acv, scored.score))
            weighted_total += scored.weighted_acv
            deal_count += 1
            if scored.score >= COMMIT_SCORE_THRESHOLD:
                commit_total += rolled.acv or 0.0
            if scored.score >= BEST_CASE_SCORE_THRESHOLD:
                best_case_total += rolled.acv or 0.0
            if opp.is_closed:
                outcome = 1 if opp.is_won else 0
                brier_pairs.append((scored.probability, outcome))
                category_outcomes.setdefault(scored.category, []).append(outcome)

    cohorts: list[CohortMetric] = []
    for as_of in sorted(weekly_cohorts):
        cohort = weekly_cohorts[as_of]
        weighted = sum(w for _, w, _ in cohort)
        actuals = sum(
            (opp.acv or 0.0)
            for opp, _, _ in cohort
            if opp.is_won and opp.close_date is not None
            and as_of <= opp.close_date <= window_end
        )
        cohorts.append(
            CohortMetric(
                cohort_week=as_of,
                deal_count=len(cohort),
                weighted_acv=weighted,
                actuals_at_close=actuals,
                mape=_mape(weighted, actuals),
            )
        )

    # actuals_total is unique wins within the window (no double-counting across
    # cohorts). overall_mape averages the non-null cohort MAPEs so per-opp
    # double-counting in weighted_total doesn't distort the headline number.
    actuals_total = sum(
        (opp.acv or 0.0)
        for opp in base_by_id.values()
        if opp.is_won and opp.close_date is not None
        and window_start <= opp.close_date <= window_end
    )
    cohort_mapes = [c.mape for c in cohorts if c.mape is not None]
    overall_mape = sum(cohort_mapes) / len(cohort_mapes) if cohort_mapes else None
    brier = _brier(brier_pairs)
    cat_hit = {
        cat: sum(outs) / len(outs) if outs else 0.0
        for cat, outs in category_outcomes.items()
    }

    return BacktestResult(
        weights_version=weights.version,
        window_start=window_start,
        window_end=window_end,
        step_days=step_days,
        cohorts=cohorts,
        overall_mape=overall_mape,
        brier_score=brier,
        category_hit_rate=cat_hit,
        deal_count=deal_count,
        actuals_total=actuals_total,
        weighted_total=weighted_total,
        commit_total=commit_total,
        best_case_total=best_case_total,
    )


def persist_backtest_result(
    result: BacktestResult,
    *,
    run_date: date,
    horizon_quarter: str | None = None,
) -> None:
    """Insert a forecast_history row. Idempotent on (run_date, horizon, version)."""
    horizon = horizon_quarter or current_quarter_label(result.window_end)
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT OR IGNORE INTO forecast_history (
                    run_date, horizon_quarter, weights_version,
                    commit_amount, best_case_amount, weighted_amount,
                    actuals_at_close, accuracy_pct, brier_score,
                    deal_count, metadata
                ) VALUES (
                    :run_date, :horizon, :version,
                    :commit, :best_case, :weighted,
                    :actuals, :accuracy, :brier,
                    :deal_count, :metadata
                )
                """
            ),
            {
                "run_date": run_date,
                "horizon": horizon,
                "version": result.weights_version,
                "commit": result.commit_total,
                "best_case": result.best_case_total,
                "weighted": result.weighted_total,
                "actuals": result.actuals_total,
                "accuracy": _accuracy_pct(result.overall_mape),
                "brier": result.brier_score,
                "deal_count": result.deal_count,
                "metadata": json.dumps(result.as_metadata()),
            },
        )


def write_backtest_report(
    result: BacktestResult,
    *,
    output_dir: Path,
    run_date: date | None = None,
) -> Path:
    """Emit a markdown report and return its path."""
    run_date = run_date or date.today()
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{run_date.isoformat()}_{result.weights_version}.md"
    lines = _render_markdown(result, run_date)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ------------------------------------------------------------------ rewind

_REWIND_FIELD_MAP: dict[str, str] = {
    "CloseDate": "close_date",
    "Amount": "amount",
    "ACV__c": "acv",
    "StageName": "stage",
    "OwnerId": "owner_id",
    "Owner.Name": "owner_name",
    "Segment__c": "segment",
    "ICP_Score__c": "icp_score",
}


def _rewind_opp(
    base: OppRecord,
    *,
    stage_changes: Iterable[StageChange],
    field_changes: Iterable[FieldChange],
    as_of: date,
) -> OppRecord | None:
    """Return `base` rolled back to its state on `as_of` (inclusive).

    Returns None when the opp was created AFTER `as_of` (no stage history
    earlier than cutoff implies it didn't exist yet).
    """
    created = base.created_date
    if created is not None:
        created_date = created.date() if hasattr(created, "date") else created
        if created_date > as_of:
            return None

    rolled: dict[str, Any] = {}

    # Undo every stage change that occurred AFTER as_of, in reverse chron.
    later_stage = [s for s in stage_changes if _as_date(s.changed_at) > as_of]
    if later_stage:
        later_stage.sort(key=lambda s: s.changed_at, reverse=True)
        earliest_revert = later_stage[-1]
        if earliest_revert.from_stage is not None:
            rolled["stage"] = earliest_revert.from_stage

    # Undo every field change that occurred AFTER as_of, pick the earliest
    # old_value (the state just before that change) per field.
    field_undo: dict[str, FieldChange] = {}
    for ch in field_changes:
        if _as_date(ch.changed_at) <= as_of:
            continue
        existing = field_undo.get(ch.field)
        if existing is None or ch.changed_at < existing.changed_at:
            field_undo[ch.field] = ch
    for api_name, ch in field_undo.items():
        attr = _REWIND_FIELD_MAP.get(api_name)
        if attr is None:
            continue
        rolled[attr] = _coerce(attr, ch.old_value)

    # Back out close-date-implied closure. If we rewound the close_date
    # forward (the opp closed after as_of), unset is_closed / is_won too.
    close_override = rolled.get("close_date", base.close_date)
    if close_override is not None and close_override > as_of and base.is_closed:
        rolled.setdefault("is_closed", False)
        rolled.setdefault("is_won", False)
        rolled.setdefault("stage", rolled.get("stage") or _infer_open_stage(base.stage))

    if not rolled:
        return base
    return replace(base, **rolled)


def _infer_open_stage(current_stage: str) -> str:
    """Fallback stage when the rewind knows the opp was open but not which stage."""
    if current_stage in {"Closed Won", "Closed Lost"}:
        return "Proposal"
    return current_stage


def _coerce(attr: str, raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    if attr == "close_date" and isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    if attr == "close_date" and isinstance(raw, datetime):
        return raw.date()
    if attr in {"amount", "acv", "icp_score"}:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return raw


def _as_date(dt: datetime | date) -> date:
    return dt.date() if isinstance(dt, datetime) else dt


def _group(items: Iterable[Any], *, key) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for item in items:
        grouped.setdefault(key(item), []).append(item)
    return grouped


def _week_steps(start: date, end: date, step_days: int) -> Iterable[date]:
    cursor = start
    while cursor <= end:
        yield cursor
        cursor = cursor + timedelta(days=step_days)


# ------------------------------------------------------------------ metrics

def _mape(predicted: float, actual: float) -> float | None:
    if actual <= 0:
        return None
    return abs(predicted - actual) / actual


def _accuracy_pct(mape: float | None) -> float | None:
    """Convert MAPE to accuracy: 1 − min(MAPE, 1). Caps at 0 for huge misses."""
    if mape is None:
        return None
    return max(0.0, 1.0 - mape)


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


# ------------------------------------------------------------------ report

def _render_markdown(result: BacktestResult, run_date: date) -> list[str]:
    lines = [
        f"# Backtest — {result.weights_version}",
        "",
        f"- **Run date**: {run_date.isoformat()}",
        f"- **Window**: {result.window_start.isoformat()} → {result.window_end.isoformat()}",
        f"- **Step**: {result.step_days} days",
        f"- **Deals scored**: {result.deal_count}",
        "",
        "## Headline metrics",
        "",
        f"- **Overall MAPE**: {_fmt_pct(result.overall_mape)}",
        f"- **Accuracy (1 − MAPE)**: {_fmt_pct(_accuracy_pct(result.overall_mape))}",
        f"- **Brier score**: {_fmt_float(result.brier_score)}",
        f"- **Weighted ACV (Σ predictions)**: {_fmt_money(result.weighted_total)}",
        f"- **Actuals at close**: {_fmt_money(result.actuals_total)}",
        f"- **Commit (score ≥ {COMMIT_SCORE_THRESHOLD})**: {_fmt_money(result.commit_total)}",
        f"- **Best Case (score ≥ {BEST_CASE_SCORE_THRESHOLD})**: {_fmt_money(result.best_case_total)}",
        "",
        "## Category hit-rate",
        "",
        "| Category | Hit rate |",
        "|---|---|",
    ]
    for _, _, category in PROBABILITY_BANDS:
        rate = result.category_hit_rate.get(category)
        lines.append(f"| {category} | {_fmt_pct(rate)} |")
    lines += [
        "",
        "## Weekly cohorts",
        "",
        "| Week | Deals | Weighted ACV | Actuals | MAPE |",
        "|---|---|---|---|---|",
    ]
    for c in result.cohorts:
        lines.append(
            f"| {c.cohort_week.isoformat()} | {c.deal_count} | "
            f"{_fmt_money(c.weighted_acv)} | {_fmt_money(c.actuals_at_close)} | "
            f"{_fmt_pct(c.mape)} |"
        )
    return lines


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _fmt_float(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.4f}"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.0f}"
