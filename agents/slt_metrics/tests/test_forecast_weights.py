"""ForecastWeights persistence — seed fallback, roundtrip, version bumps,
audit trail.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from agents.slt_metrics.forecast import weights as w
from agents.slt_metrics.pipeline.config import WEIGHT_SEEDS
from agents.slt_metrics.types import ForecastWeights
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _clean_forecast_history():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM forecast_history"))
    yield


def test_get_current_returns_seed_when_table_empty():
    assert w.get_current_weights() == WEIGHT_SEEDS


def test_save_and_get_roundtrip():
    bumped = w.bump_version(WEIGHT_SEEDS, "tuned")
    new_weights = ForecastWeights(
        icp=0.20, stage=0.35, activity=0.15, timeline=0.15, call=0.15,
        version=bumped.version,
    )
    row_id = w.save_weights(
        new_weights,
        justification="bumped ICP down after backtest",
        approval_gate_id=42,
    )
    assert row_id > 0

    loaded = w.get_current_weights()
    assert loaded.version == bumped.version
    assert loaded.icp == 0.20
    assert loaded.stage == 0.35


def test_save_rejects_non_unit_sum():
    bad = ForecastWeights(icp=0.5, stage=0.5, activity=0.5, timeline=0.0, call=0.0)
    with pytest.raises(ValueError, match="do not sum to 1"):
        w.save_weights(bad, justification="invalid")


def test_latest_wins_when_multiple_versions_saved():
    a = ForecastWeights(
        icp=0.20, stage=0.35, activity=0.15, timeline=0.15, call=0.15,
        version="v2-tuned-2026-04-10",
    )
    b = ForecastWeights(
        icp=0.30, stage=0.30, activity=0.15, timeline=0.10, call=0.15,
        version="v3-tuned-2026-04-13",
    )
    w.save_weights(a, justification="first", run_date=date(2026, 4, 10))
    w.save_weights(b, justification="second", run_date=date(2026, 4, 13))

    current = w.get_current_weights()
    assert current.version == "v3-tuned-2026-04-13"
    assert current.icp == 0.30


def test_list_versions_orders_descending():
    w.save_weights(
        ForecastWeights(icp=0.25, stage=0.30, activity=0.15, timeline=0.15, call=0.15,
                        version="v2-a"),
        justification="a", run_date=date(2026, 4, 10),
    )
    w.save_weights(
        ForecastWeights(icp=0.25, stage=0.30, activity=0.15, timeline=0.15, call=0.15,
                        version="v3-b"),
        justification="b", run_date=date(2026, 4, 12),
    )
    versions = w.list_versions()
    assert [v["version"] for v in versions] == ["v3-b", "v2-a"]
    assert versions[0]["justification"] == "b"


def test_bump_version_increments_prefix():
    seeded = w.bump_version(WEIGHT_SEEDS, "tuned")
    assert seeded.version.startswith("v2-tuned-")
    again = w.bump_version(seeded, "tuned")
    assert again.version.startswith("v3-tuned-")


def test_bump_version_handles_nonstandard_previous():
    ugly = ForecastWeights(
        icp=0.25, stage=0.30, activity=0.15, timeline=0.15, call=0.15,
        version="custom",
    )
    bumped = w.bump_version(ugly, "manual")
    assert bumped.version.startswith("v2-manual-")


def test_get_current_tolerates_garbage_metadata():
    # Insert a weights_update row with unparseable metadata directly.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO forecast_history (run_date, horizon_quarter, "
                "weights_version, commit_amount, best_case_amount, "
                "weighted_amount, deal_count, metadata) "
                "VALUES ('2026-04-13', 'FY-CURRENT', 'v2-broken', 0, 0, 0, 0, :m)"
            ),
            {"m": '{"kind":"weights_update", not-json'},
        )
    assert w.get_current_weights() == WEIGHT_SEEDS
