"""Migration 0004 — creates pipeline_snapshots + forecast_history with indexes.

Python module names can't start with a digit, so we load the migration via
importlib instead of a plain `from ... import`.
"""
from __future__ import annotations

import importlib

from sqlalchemy import text

from shared.db.connection import get_engine

m = importlib.import_module("shared.db.migrations.versions.0004_slt_revenue_metrics")


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
    ).fetchone()
    return row is not None


def _index_names(conn, table: str) -> set[str]:
    rows = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:t"), {"t": table}
    ).fetchall()
    return {r[0] for r in rows}


def test_upgrade_creates_pipeline_snapshots():
    m.upgrade()
    engine = get_engine()
    with engine.begin() as conn:
        assert _table_exists(conn, "pipeline_snapshots")
        cols = {
            r[1] for r in conn.execute(
                text("PRAGMA table_info(pipeline_snapshots)")
            ).fetchall()
        }
    expected = {
        "id", "snapshot_date", "opp_id", "stage", "amount", "acv",
        "close_date", "owner_id", "owner_name", "account_id", "segment",
        "score", "category", "probability", "weighted_acv", "metadata",
        "created_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_upgrade_creates_forecast_history():
    m.upgrade()
    engine = get_engine()
    with engine.begin() as conn:
        assert _table_exists(conn, "forecast_history")
        cols = {
            r[1] for r in conn.execute(
                text("PRAGMA table_info(forecast_history)")
            ).fetchall()
        }
    expected = {
        "id", "run_date", "horizon_quarter", "weights_version",
        "commit_amount", "best_case_amount", "weighted_amount",
        "actuals_at_close", "accuracy_pct", "brier_score", "deal_count",
        "metadata", "created_at",
    }
    assert expected.issubset(cols), f"missing cols: {expected - cols}"


def test_expected_indexes_exist():
    m.upgrade()
    engine = get_engine()
    with engine.begin() as conn:
        snap_idx = _index_names(conn, "pipeline_snapshots")
        fh_idx = _index_names(conn, "forecast_history")
    assert {"idx_snap_date", "idx_snap_owner", "idx_snap_close", "idx_snap_opp"}.issubset(snap_idx)
    assert {"idx_fh_quarter"}.issubset(fh_idx)


def test_unique_constraint_snapshot_date_opp_id():
    m.upgrade()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (snapshot_date, opp_id, stage) "
                "VALUES ('2026-04-13', 'OPP_UNIQ_TEST', 'Proposal')"
            )
        )
    import sqlalchemy.exc
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO pipeline_snapshots (snapshot_date, opp_id, stage) "
                    "VALUES ('2026-04-13', 'OPP_UNIQ_TEST', 'Pilot')"
                )
            )
        assert False, "expected IntegrityError on duplicate (snapshot_date, opp_id)"
    except sqlalchemy.exc.IntegrityError:
        pass
    # Same opp on a different date is fine
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO pipeline_snapshots (snapshot_date, opp_id, stage) "
                "VALUES ('2026-04-14', 'OPP_UNIQ_TEST', 'Pilot')"
            )
        )


def test_unique_constraint_forecast_history_triple():
    m.upgrade()
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO forecast_history "
                "(run_date, horizon_quarter, weights_version, commit_amount) "
                "VALUES ('2026-04-13', 'FY2026-Q2', 'v1-seed', 1000000)"
            )
        )
    import sqlalchemy.exc
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO forecast_history "
                    "(run_date, horizon_quarter, weights_version, commit_amount) "
                    "VALUES ('2026-04-13', 'FY2026-Q2', 'v1-seed', 2000000)"
                )
            )
        assert False, "expected IntegrityError on duplicate triple"
    except sqlalchemy.exc.IntegrityError:
        pass


def test_downgrade_drops_both_tables():
    m.upgrade()
    m.downgrade()
    engine = get_engine()
    with engine.begin() as conn:
        assert not _table_exists(conn, "pipeline_snapshots")
        assert not _table_exists(conn, "forecast_history")
    # Re-upgrade so later tests (in other modules) that assume these tables exist pass.
    m.upgrade()


def test_migration_metadata():
    assert m.revision == "0004_slt_revenue_metrics"
    assert m.down_revision == "0003_cs_agent"


def test_slt_draft_review_tier_registered():
    from shared.governance import APPROVAL_TIERS
    assert "slt_draft_review" in APPROVAL_TIERS
    tier = APPROVAL_TIERS["slt_draft_review"]
    assert tier.gate == "slack_button"
    assert tier.approver == "o_only"
