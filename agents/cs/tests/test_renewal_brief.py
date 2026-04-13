"""M6 — Renewal brief markdown generator tests."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from agents.cs.renewal import brief
from shared.db.connection import get_engine


class FakeSf:
    def __init__(self, renewal=None, cases=None):
        self._renewal = renewal
        self._cases = cases or []

    def soql_query(self, q, **_):
        if "Type = 'Renewal'" in q:
            return {"records": [self._renewal] if self._renewal else []}
        if "FROM Case" in q:
            return {"records": self._cases}
        return {"records": []}


class FakeFireflies:
    def __init__(self, calls=None):
        self._calls = calls or []
    def list_transcripts(self, **_): return self._calls


def _seed_health(account_id: str, score=70, nps=8, cat="passive", name="Acme"):
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_account_health WHERE account_id = :a"), {"a": account_id})
        conn.execute(
            text(
                """INSERT INTO cs_account_health
                     (account_id, name, score, nps_score, nps_category, nps_at, checked_at)
                   VALUES (:a, :n, :s, :nps, :c, :t, :t)"""
            ),
            {"a": account_id, "n": name, "s": score, "nps": nps, "c": cat, "t": now},
        )


def _seed_risk(account_id: str, score=75, tier=70):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_churn_risk WHERE account_id = :a"), {"a": account_id})
        conn.execute(
            text(
                """INSERT INTO cs_churn_risk (account_id, score, tier, factors_json, created_at)
                   VALUES (:a, :s, :t, :f, :n)"""
            ),
            {
                "a": account_id,
                "s": score,
                "t": tier,
                "f": json.dumps({
                    "factors": {"health_drop": 1.0},
                    "contributions": {"health_drop": 35, "nps": 4, "case_spike": 15},
                }),
                "n": datetime.now(timezone.utc),
            },
        )


def test_brief_renders_core_sections():
    _seed_health("001A", score=72, nps=7, cat="passive", name="Beta Co")
    sf = FakeSf(
        renewal={
            "Id": "006R",
            "Name": "Beta Co Renewal 2026",
            "StageName": "Renewal Outreach",
            "Amount": 42000,
            "Zen_Contract_End_Date__c": "2026-08-10",
        },
        cases=[{"Id": "500X", "Subject": "Login bug", "Priority": "High", "CreatedDate": "2026-04-01T00:00:00Z"}],
    )
    ff = FakeFireflies(calls=[{"date": "2026-04-05", "title": "Beta Co weekly sync"}])

    md = brief.generate("001A", sf_mcp=sf, fireflies_mcp=ff)
    assert "Renewal brief — Beta Co" in md
    assert "Vitally score: **72" in md  # float: 72 or 72.0 both fine
    assert "Beta Co Renewal 2026" in md
    assert "Beta Co weekly sync" in md
    assert "Login bug" in md
    assert "Suggested talking points" in md


def test_brief_surfaces_risk_when_tiered():
    _seed_health("001B", name="Gamma")
    _seed_risk("001B", score=88, tier=85)
    md = brief.generate("001B", sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies())
    assert "Churn risk" in md
    assert "tier 85" in md
    assert "Churn risk elevated" in md  # tier ≥70 triggers emphasis


def test_brief_handles_missing_renewal_and_calls():
    _seed_health("001C", name="Delta")
    md = brief.generate("001C", sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies())
    assert "No open Renewal opportunity" in md
    assert "No recent calls in Fireflies" in md
    assert "No open cases" in md


def test_brief_uses_account_id_when_name_missing():
    md = brief.generate("001UNKNOWN", sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies())
    assert "Renewal brief — 001UNKNOWN" in md
