"""Local sales_reps rate gates — hard raise, soft warn, unknown bucket."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from agents.sales_reps import rate_gates
from shared.db.connection import get_engine


def _reset_bucket(bucket: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM rate_limits WHERE bucket = :b"), {"b": bucket})


def test_unknown_bucket_raises():
    with pytest.raises(ValueError):
        rate_gates.check("definitely_not_a_bucket")


def test_limit_for_returns_configured_value():
    assert rate_gates.limit_for("sales_reps_grader_hourly") == 100


def test_list_buckets_has_expected_keys():
    buckets = set(rate_gates.list_buckets())
    assert "sales_reps_grader_hourly" in buckets
    assert "sales_reps_coaching_dm_daily" in buckets
    assert "sales_reps_sync_alert_hourly" in buckets


def test_hard_mode_raises_over_limit(monkeypatch):
    _reset_bucket("sales_reps_sync_alert_hourly")
    # Bump the limit down to 2 for this test.
    monkeypatch.setitem(rate_gates._LIMITS, "sales_reps_sync_alert_hourly", 2)

    rate_gates.check("sales_reps_sync_alert_hourly")
    rate_gates.check("sales_reps_sync_alert_hourly")
    with pytest.raises(rate_gates.RateGateExceeded):
        rate_gates.check("sales_reps_sync_alert_hourly")


def test_soft_mode_warns_instead_of_raising(monkeypatch, caplog):
    _reset_bucket("sales_reps_coaching_dm_daily")
    monkeypatch.setitem(rate_gates._LIMITS, "sales_reps_coaching_dm_daily", 1)

    rate_gates.check("sales_reps_coaching_dm_daily", mode="soft")
    with caplog.at_level("WARNING"):
        rate_gates.check("sales_reps_coaching_dm_daily", mode="soft")
    assert any("SOFT breach" in r.message for r in caplog.records)
