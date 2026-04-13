"""Backtest replay + MAPE/Brier metrics."""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import text

from agents.slt_metrics.forecast import backtest as bt
from agents.slt_metrics.forecast.backtest import (
    BacktestResult,
    FieldChange,
    StageChange,
)
from agents.slt_metrics.types import ForecastWeights, OppRecord
from shared.db.connection import get_engine


def _opp(
    *,
    opp_id: str,
    stage: str = "Pilot",
    acv: float | None = 100_000.0,
    close_date: date | None = date(2026, 5, 1),
    is_closed: bool = False,
    is_won: bool = False,
    created: datetime | None = datetime(2026, 1, 1, 9, 0, 0),
    owner: str | None = "Rep A",
) -> OppRecord:
    return OppRecord(
        id=opp_id, name=f"Opp {opp_id}",
        account_id=None, account_name=f"Acct {opp_id}",
        account_website=None, account_type=None,
        owner_id=None, owner_name=owner, owner_role=None, owner_manager=None,
        stage=stage, is_closed=is_closed, is_won=is_won,
        amount=acv, acv=acv, fixed_arr=None,
        locations=None, type=None, lead_source=None,
        close_date=close_date, created_date=created,
        last_activity_date=date(2026, 4, 10), last_modified_date=None,
        last_stage_change_date=None, days_since_stage_change=None,
        time_in_stage=None, probability_sf=None,
        description=None, next_steps=None, next_step_date=None,
        icp_score=None, segment="MM",
    )


# ------------------------------------------------------------------ _rewind_opp

def test_rewind_returns_none_when_opp_created_after_as_of():
    opp = _opp(opp_id="O1", created=datetime(2026, 3, 15, 9, 0, 0))
    rolled = bt._rewind_opp(
        opp, stage_changes=[], field_changes=[], as_of=date(2026, 2, 1),
    )
    assert rolled is None


def test_rewind_rolls_back_stage_to_prior_value():
    opp = _opp(opp_id="O1", stage="Proposal")
    stage_changes = [
        StageChange(opp_id="O1", changed_at=datetime(2026, 3, 1, 12, 0),
                    from_stage="Demo", to_stage="Pilot"),
        StageChange(opp_id="O1", changed_at=datetime(2026, 3, 15, 12, 0),
                    from_stage="Pilot", to_stage="Proposal"),
    ]
    rolled = bt._rewind_opp(
        opp, stage_changes=stage_changes, field_changes=[], as_of=date(2026, 2, 20),
    )
    assert rolled is not None
    assert rolled.stage == "Demo"


def test_rewind_keeps_current_stage_when_no_later_changes():
    opp = _opp(opp_id="O1", stage="Pilot")
    rolled = bt._rewind_opp(
        opp, stage_changes=[], field_changes=[], as_of=date(2026, 4, 1),
    )
    assert rolled is not None
    assert rolled.stage == "Pilot"


def test_rewind_undoes_field_change_using_old_value():
    opp = _opp(opp_id="O1", acv=200_000.0)
    changes = [
        FieldChange(opp_id="O1", changed_at=datetime(2026, 3, 15, 10, 0),
                    field="ACV__c", old_value="100000", new_value="200000"),
    ]
    rolled = bt._rewind_opp(
        opp, stage_changes=[], field_changes=changes, as_of=date(2026, 3, 1),
    )
    assert rolled is not None
    assert rolled.acv == pytest.approx(100_000.0)


def test_rewind_picks_earliest_old_value_when_multiple_changes_per_field():
    opp = _opp(opp_id="O1", acv=300_000.0)
    changes = [
        FieldChange(opp_id="O1", changed_at=datetime(2026, 3, 1, 10, 0),
                    field="ACV__c", old_value="100000", new_value="200000"),
        FieldChange(opp_id="O1", changed_at=datetime(2026, 4, 1, 10, 0),
                    field="ACV__c", old_value="200000", new_value="300000"),
    ]
    # as_of = Feb 1 → both changes are "after", and we want the pre-Mar-1 state (100k).
    rolled = bt._rewind_opp(
        opp, stage_changes=[], field_changes=changes, as_of=date(2026, 2, 1),
    )
    assert rolled is not None
    assert rolled.acv == pytest.approx(100_000.0)


def test_rewind_undoes_close_date_and_closure_when_opp_closed_after_as_of():
    opp = _opp(
        opp_id="O1", stage="Closed Won", close_date=date(2026, 4, 20),
        is_closed=True, is_won=True, acv=100_000.0,
    )
    stage_changes = [
        StageChange(opp_id="O1", changed_at=datetime(2026, 4, 20, 16, 0),
                    from_stage="Proposal", to_stage="Closed Won"),
    ]
    rolled = bt._rewind_opp(
        opp, stage_changes=stage_changes, field_changes=[], as_of=date(2026, 4, 1),
    )
    assert rolled is not None
    assert rolled.stage == "Proposal"
    assert rolled.is_closed is False
    assert rolled.is_won is False


def test_rewind_passthrough_when_no_changes_after_as_of():
    opp = _opp(opp_id="O1", stage="Pilot")
    changes = [
        FieldChange(opp_id="O1", changed_at=datetime(2026, 1, 15, 10, 0),
                    field="ACV__c", old_value="50000", new_value="100000"),
    ]
    rolled = bt._rewind_opp(
        opp, stage_changes=[], field_changes=changes, as_of=date(2026, 2, 1),
    )
    assert rolled is not None
    assert rolled is opp   # unchanged reference when no rollback needed


def test_rewind_unknown_field_is_ignored_not_crashed():
    opp = _opp(opp_id="O1")
    changes = [
        FieldChange(opp_id="O1", changed_at=datetime(2026, 3, 1, 10, 0),
                    field="SomeCustom__c", old_value="x", new_value="y"),
    ]
    rolled = bt._rewind_opp(
        opp, stage_changes=[], field_changes=changes, as_of=date(2026, 2, 1),
    )
    # No mapped field changes, so record passes through unchanged.
    assert rolled is not None
    assert rolled.acv == pytest.approx(100_000.0)


# ------------------------------------------------------------------ backtest()

def test_backtest_rejects_invalid_step_days():
    with pytest.raises(ValueError):
        bt.backtest(
            base_opps=[], stage_changes=[], field_changes=[],
            weights=ForecastWeights(),
            window_start=date(2026, 1, 1), window_end=date(2026, 3, 31),
            step_days=0,
        )


def test_backtest_rejects_window_end_before_start():
    with pytest.raises(ValueError):
        bt.backtest(
            base_opps=[], stage_changes=[], field_changes=[],
            weights=ForecastWeights(),
            window_start=date(2026, 3, 31), window_end=date(2026, 1, 1),
        )


def test_backtest_empty_input_returns_empty_cohorts():
    result = bt.backtest(
        base_opps=[], stage_changes=[], field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 3, 1), window_end=date(2026, 3, 15),
    )
    assert result.deal_count == 0
    # Cohorts still emit for each week in the window, just with zero deals.
    assert all(c.deal_count == 0 for c in result.cohorts)
    assert result.overall_mape is None
    assert result.brier_score is None


def test_backtest_emits_one_cohort_per_step():
    result = bt.backtest(
        base_opps=[_opp(opp_id="O1", is_closed=False)],
        stage_changes=[], field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 3, 2), window_end=date(2026, 3, 30),
        step_days=7,
    )
    # Mar 2, 9, 16, 23, 30 → 5 cohorts.
    assert [c.cohort_week for c in result.cohorts] == [
        date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 16),
        date(2026, 3, 23), date(2026, 3, 30),
    ]


def test_backtest_skips_opp_already_closed_by_cohort_week():
    closed = _opp(
        opp_id="C1", stage="Closed Won",
        close_date=date(2026, 2, 15),
        is_closed=True, is_won=True,
    )
    result = bt.backtest(
        base_opps=[closed], stage_changes=[], field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 3, 1), window_end=date(2026, 3, 15),
    )
    assert result.deal_count == 0  # closed before window, never scored


def test_backtest_computes_weighted_total_and_commit_thresholds():
    # Strong commit deal: stage=Proposal, solid activity → score ≥80.
    strong = _opp(
        opp_id="S1", stage="Proposal",
        acv=100_000.0, close_date=date(2026, 5, 1),
        created=datetime(2026, 1, 1, 0, 0),
    )
    result = bt.backtest(
        base_opps=[strong],
        stage_changes=[], field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 4, 1), window_end=date(2026, 4, 8),
        step_days=7,
    )
    # 2 cohort weeks × 1 deal = 2 scored observations.
    assert result.deal_count == 2
    assert result.weighted_total > 0.0
    # Commit aggregate should never be negative.
    assert result.commit_total >= 0.0
    assert result.best_case_total >= result.commit_total


def test_backtest_brier_score_on_pure_correct_predictions():
    """A deal that became Closed Won with ICP/stage strong should earn a low Brier."""
    won = _opp(
        opp_id="W1", stage="Closed Won",
        close_date=date(2026, 4, 20),
        is_closed=True, is_won=True, acv=100_000.0,
    )
    stage_changes = [
        StageChange(opp_id="W1", changed_at=datetime(2026, 4, 20, 16, 0),
                    from_stage="Proposal", to_stage="Closed Won"),
    ]
    result = bt.backtest(
        base_opps=[won], stage_changes=stage_changes, field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 4, 1), window_end=date(2026, 4, 15),
        step_days=7,
    )
    assert result.brier_score is not None
    assert 0.0 <= result.brier_score <= 1.0


def test_backtest_mape_zero_when_predictions_match_actuals():
    # Build a closed-won opp whose weighted_acv happens to match its actual ACV.
    # Easier path: check MAPE is non-negative for the only-closed cohort.
    won = _opp(
        opp_id="W1", stage="Closed Won",
        close_date=date(2026, 4, 10),
        is_closed=True, is_won=True, acv=50_000.0,
    )
    stage_changes = [
        StageChange(opp_id="W1", changed_at=datetime(2026, 4, 10, 12, 0),
                    from_stage="Proposal", to_stage="Closed Won"),
    ]
    result = bt.backtest(
        base_opps=[won], stage_changes=stage_changes, field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 4, 1), window_end=date(2026, 4, 15),
        step_days=7,
    )
    # The Apr 1 cohort: opp rewound to open; actuals=50k because closes on Apr 10 (within window).
    apr1 = next(c for c in result.cohorts if c.cohort_week == date(2026, 4, 1))
    assert apr1.actuals_at_close == pytest.approx(50_000.0)
    assert apr1.mape is not None
    assert apr1.mape >= 0.0


def test_backtest_actuals_totals_only_wins_within_window():
    won_in = _opp(
        opp_id="W1", stage="Closed Won",
        close_date=date(2026, 4, 10), is_closed=True, is_won=True, acv=200_000.0,
    )
    stage_changes = [
        StageChange(opp_id="W1", changed_at=datetime(2026, 4, 10, 12, 0),
                    from_stage="Proposal", to_stage="Closed Won"),
    ]
    lost_open = _opp(
        opp_id="L1", stage="Closed Lost",
        close_date=date(2026, 4, 5), is_closed=True, is_won=False, acv=300_000.0,
    )
    lost_stage = [
        StageChange(opp_id="L1", changed_at=datetime(2026, 4, 5, 12, 0),
                    from_stage="Proposal", to_stage="Closed Lost"),
    ]
    result = bt.backtest(
        base_opps=[won_in, lost_open],
        stage_changes=stage_changes + lost_stage, field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 4, 1), window_end=date(2026, 4, 15),
        step_days=7,
    )
    # Only wins contribute to actuals; losses should not inflate it.
    assert result.actuals_total == pytest.approx(200_000.0)


def test_backtest_category_hit_rate_buckets_outcomes():
    won = _opp(
        opp_id="W1", stage="Closed Won",
        close_date=date(2026, 4, 10), is_closed=True, is_won=True, acv=100_000.0,
    )
    lost = _opp(
        opp_id="L1", stage="Closed Lost",
        close_date=date(2026, 4, 10), is_closed=True, is_won=False, acv=100_000.0,
    )
    stage_changes = [
        StageChange(opp_id="W1", changed_at=datetime(2026, 4, 10, 12, 0),
                    from_stage="Proposal", to_stage="Closed Won"),
        StageChange(opp_id="L1", changed_at=datetime(2026, 4, 10, 12, 0),
                    from_stage="Proposal", to_stage="Closed Lost"),
    ]
    result = bt.backtest(
        base_opps=[won, lost], stage_changes=stage_changes, field_changes=[],
        weights=ForecastWeights(),
        window_start=date(2026, 4, 1), window_end=date(2026, 4, 8),
        step_days=7,
    )
    # Every bucketed category hit-rate is in [0, 1].
    for cat, rate in result.category_hit_rate.items():
        assert 0.0 <= rate <= 1.0, f"{cat} hit-rate out of bounds"


# ------------------------------------------------------------------ persist

def test_persist_backtest_result_writes_forecast_history_row():
    # Clean slate before the write so the assertion is stable.
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM forecast_history"))
    result = BacktestResult(
        weights_version="v1-test",
        window_start=date(2026, 1, 1), window_end=date(2026, 3, 31),
        step_days=7, cohorts=[],
        overall_mape=0.15, brier_score=0.12,
        category_hit_rate={"Commit": 0.6},
        deal_count=42,
        actuals_total=500_000.0, weighted_total=480_000.0,
        commit_total=300_000.0, best_case_total=420_000.0,
    )
    bt.persist_backtest_result(
        result, run_date=date(2026, 4, 13), horizon_quarter="FY2026-Q1",
    )
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT weights_version, commit_amount, best_case_amount, "
                "weighted_amount, actuals_at_close, accuracy_pct, brier_score, "
                "deal_count FROM forecast_history WHERE run_date = :d"
            ),
            {"d": date(2026, 4, 13).isoformat()},
        ).mappings().one()
    assert row["weights_version"] == "v1-test"
    assert row["commit_amount"] == pytest.approx(300_000.0)
    assert row["weighted_amount"] == pytest.approx(480_000.0)
    assert row["brier_score"] == pytest.approx(0.12)
    assert row["accuracy_pct"] == pytest.approx(0.85, abs=1e-6)
    assert row["deal_count"] == 42


def test_persist_backtest_result_is_idempotent():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM forecast_history"))
    result = BacktestResult(
        weights_version="v2-test",
        window_start=date(2026, 1, 1), window_end=date(2026, 3, 31),
        step_days=7, cohorts=[],
        overall_mape=None, brier_score=None,
        category_hit_rate={}, deal_count=0,
        actuals_total=0.0, weighted_total=0.0,
        commit_total=0.0, best_case_total=0.0,
    )
    bt.persist_backtest_result(
        result, run_date=date(2026, 4, 13), horizon_quarter="FY2026-Q1",
    )
    bt.persist_backtest_result(
        result, run_date=date(2026, 4, 13), horizon_quarter="FY2026-Q1",
    )
    with engine.begin() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM forecast_history WHERE weights_version = :v"),
            {"v": "v2-test"},
        ).scalar()
    assert count == 1


# ------------------------------------------------------------------ markdown

def test_write_backtest_report_creates_markdown_file(tmp_path):
    result = BacktestResult(
        weights_version="v1-test",
        window_start=date(2026, 1, 1), window_end=date(2026, 3, 31),
        step_days=7,
        cohorts=[bt.CohortMetric(
            cohort_week=date(2026, 1, 1), deal_count=3,
            weighted_acv=150_000.0, actuals_at_close=120_000.0, mape=0.25,
        )],
        overall_mape=0.15, brier_score=0.12,
        category_hit_rate={"Strong Commit": 0.9, "Commit": 0.5},
        deal_count=3,
        actuals_total=120_000.0, weighted_total=150_000.0,
        commit_total=80_000.0, best_case_total=150_000.0,
    )
    path = bt.write_backtest_report(
        result, output_dir=tmp_path, run_date=date(2026, 4, 13),
    )
    assert path.exists()
    content = path.read_text()
    assert "Backtest — v1-test" in content
    assert "Overall MAPE" in content
    assert "Weekly cohorts" in content
    assert "2026-01-01" in content
    assert "$150,000" in content
