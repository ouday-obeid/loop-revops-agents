"""Migration 0007 + rep_forecast_store upsert / read behavior."""
from __future__ import annotations

import importlib
from datetime import datetime

import pytest
from sqlalchemy import text

from agents.slt_metrics.pipeline import rep_forecast_store
from agents.slt_metrics.types import RepForecastEntry
from shared.db.connection import get_engine


m = importlib.import_module("shared.db.migrations.versions.0007_rep_forecasts")


@pytest.fixture(autouse=True)
def _clean_rep_forecasts():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM rep_forecasts"))
    yield


# ---------------------------------------------------------------- migration

def test_migration_0007_creates_rep_forecasts_table():
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='rep_forecasts'")
        ).fetchone()
    assert row is not None


def test_migration_0007_columns_and_pk():
    engine = get_engine()
    with engine.begin() as conn:
        cols = {
            r[1]: (r[2], r[5]) for r in conn.execute(
                text("PRAGMA table_info(rep_forecasts)")
            ).fetchall()
        }
    assert set(cols) == {
        "owner_name", "quarter", "commit_acv", "best_case_acv",
        "notes", "source", "submitted_at",
    }
    # owner_name + quarter are both PK components (pk index > 0).
    assert cols["owner_name"][1] > 0
    assert cols["quarter"][1] > 0


def test_migration_0007_quarter_index_exists():
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='rep_forecasts'")
        ).fetchall()
    names = {r[0] for r in rows}
    assert "idx_rep_forecasts_quarter" in names


def test_migration_0007_roundtrip_idempotent():
    # Re-running upgrade should not fail (IF NOT EXISTS guards all statements).
    m.upgrade()
    m.upgrade()


# ---------------------------------------------------------------- upsert_rep_forecasts

def test_upsert_inserts_new_rows():
    entries = [
        RepForecastEntry(
            owner_name="Sarra Herlich", quarter="FY2026-Q2",
            commit_acv=250_000.0, best_case_acv=400_000.0, notes="ramping",
        ),
        RepForecastEntry(
            owner_name="Alex Reyes", quarter="FY2026-Q2",
            commit_acv=350_000.0, best_case_acv=500_000.0, notes=None,
        ),
    ]
    written = rep_forecast_store.upsert_rep_forecasts(entries, source="april-17.csv")
    assert written == 2

    stored = rep_forecast_store.read_rep_forecasts("FY2026-Q2")
    assert set(stored) == {"Sarra Herlich", "Alex Reyes"}
    assert stored["Sarra Herlich"].commit_acv == 250_000.0
    assert stored["Sarra Herlich"].source == "april-17.csv"
    assert stored["Alex Reyes"].best_case_acv == 500_000.0


def test_upsert_second_submission_overwrites_same_quarter():
    first = RepForecastEntry(
        owner_name="Sarra Herlich", quarter="FY2026-Q2",
        commit_acv=100_000.0, best_case_acv=200_000.0,
    )
    second = RepForecastEntry(
        owner_name="Sarra Herlich", quarter="FY2026-Q2",
        commit_acv=300_000.0, best_case_acv=500_000.0, notes="revised",
    )
    rep_forecast_store.upsert_rep_forecasts([first], source="v1.csv")
    rep_forecast_store.upsert_rep_forecasts([second], source="v2.csv")

    stored = rep_forecast_store.read_rep_forecasts("FY2026-Q2")
    assert len(stored) == 1
    row = stored["Sarra Herlich"]
    assert row.commit_acv == 300_000.0
    assert row.best_case_acv == 500_000.0
    assert row.notes == "revised"
    assert row.source == "v2.csv"


def test_upsert_keeps_different_quarters_separate():
    entries = [
        RepForecastEntry("Sarra Herlich", "FY2026-Q1", 100_000.0, 150_000.0),
        RepForecastEntry("Sarra Herlich", "FY2026-Q2", 250_000.0, 400_000.0),
    ]
    rep_forecast_store.upsert_rep_forecasts(entries)

    q1 = rep_forecast_store.read_rep_forecasts("FY2026-Q1")
    q2 = rep_forecast_store.read_rep_forecasts("FY2026-Q2")
    assert q1["Sarra Herlich"].commit_acv == 100_000.0
    assert q2["Sarra Herlich"].commit_acv == 250_000.0


def test_upsert_empty_is_noop():
    assert rep_forecast_store.upsert_rep_forecasts([]) == 0
    assert rep_forecast_store.read_rep_forecasts("FY2026-Q2") == {}


def test_upsert_preserves_provided_submitted_at():
    pinned = datetime(2026, 4, 17, 9, 0, 0)
    entry = RepForecastEntry(
        owner_name="Sarra Herlich", quarter="FY2026-Q2",
        commit_acv=200_000.0, best_case_acv=300_000.0,
        submitted_at=pinned,
    )
    rep_forecast_store.upsert_rep_forecasts([entry])
    stored = rep_forecast_store.read_rep_forecasts("FY2026-Q2")
    assert stored["Sarra Herlich"].submitted_at == pinned


# ---------------------------------------------------------------- read_rep_forecasts

def test_read_empty_quarter_returns_empty_dict():
    assert rep_forecast_store.read_rep_forecasts("FY2099-Q4") == {}
