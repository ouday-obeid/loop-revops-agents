"""M6 — QBR markdown generator tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from agents.cs.qbr import generator
from shared.db.connection import get_engine


class FakeSf:
    def __init__(self, cases_total=0, cases_closed=0, renewal=None):
        self._t, self._c = cases_total, cases_closed
        self._renewal = renewal

    def soql_query(self, q, **_):
        if "IsClosed = true" in q:
            return {"totalSize": self._c, "records": [{"c": self._c}]}
        if "FROM Case" in q:
            return {"totalSize": self._t, "records": [{"c": self._t}]}
        if "FROM Opportunity" in q:
            return {"records": [self._renewal] if self._renewal else []}
        return {"records": []}


class FakeFireflies:
    def __init__(self, calls=None): self._calls = calls or []
    def list_transcripts(self, **_): return self._calls


def _seed_history(account_id: str, scores_over_days: list[tuple[float, int]]):
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_account_health_history WHERE account_id = :a"), {"a": account_id})
        for score, days_ago in scores_over_days:
            conn.execute(
                text(
                    """INSERT INTO cs_account_health_history (account_id, score, checked_at)
                       VALUES (:a, :s, :t)"""
                ),
                {"a": account_id, "s": score, "t": now - timedelta(days=days_ago)},
            )


def test_qbr_declining_trend_flagged():
    _seed_history("001A", [(90, 80), (80, 40), (60, 5)])
    md = generator.generate("001A", sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies())
    assert "Trajectory: **declining**" in md
    assert "Health declining" in md


def test_qbr_improving_trend_labeled():
    _seed_history("001B", [(50, 80), (70, 40), (85, 5)])
    md = generator.generate("001B", sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies())
    assert "Trajectory: **improving**" in md


def test_qbr_stable_trend_labeled():
    _seed_history("001C", [(70, 80), (72, 40), (71, 5)])
    md = generator.generate("001C", sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies())
    assert "Trajectory: **stable**" in md


def test_qbr_case_volumes_rendered():
    sf = FakeSf(cases_total=8, cases_closed=5)
    md = generator.generate("001D", sf_mcp=sf, fireflies_mcp=FakeFireflies())
    assert "Created: **8**" in md
    assert "Closed: 5" in md
    assert "Still open: 3" in md


def test_qbr_call_cadence_low_flagged():
    md = generator.generate(
        "001E",
        sf_mcp=FakeSf(),
        fireflies_mcp=FakeFireflies([{"date": "2026-03-01", "title": "One-off"}]),
    )
    assert "Call cadence low" in md


def test_qbr_includes_renewal_when_present():
    sf = FakeSf(renewal={
        "Id": "006R", "Name": "Acme Renewal", "StageName": "Negotiation",
        "Amount": 60000, "Zen_Contract_End_Date__c": "2026-11-30",
    })
    md = generator.generate("001F", sf_mcp=sf, fireflies_mcp=FakeFireflies())
    assert "Acme Renewal" in md
    assert "Negotiation" in md
