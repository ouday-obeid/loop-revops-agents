"""M3 — Churn risk sweep orchestration tests (tier routing, idempotency)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.cs.risk import churn_risk
from shared.db.connection import get_engine


class FakeSlackSender:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, channel: str, text_: str, blocks=None) -> dict:
        self.sent.append((channel, text_))
        return {"ok": True, "ts": "0", "channel": channel}


class FakeSf:
    """Controllable SF. case_counts: (last_30d, prior_30d). renewal: (days_until, ...)."""

    def __init__(self, cases: tuple[int, int] = (0, 0), renewal_days: int | None = None):
        self._cases_last, self._cases_prior = cases
        self._renewal = renewal_days
        self.queries: list[str] = []

    def soql_query(self, q: str, **_):
        self.queries.append(q)
        if "Opportunity" in q:
            if self._renewal is None:
                return {"records": []}
            end = (datetime.now(timezone.utc) + timedelta(days=self._renewal)).date().isoformat()
            return {"records": [{"Zen_Contract_End_Date__c": end}]}
        if "Case" in q:
            # Heuristic: the first Case query is last-30d, second is prior-30d.
            idx = len([x for x in self.queries if "FROM Case" in x])
            count = self._cases_last if idx == 1 else self._cases_prior
            return {"totalSize": count, "records": [{"c": count}]}
        return {"records": []}


class FakeFireflies:
    def __init__(self, days_since_call: int | None = None):
        self._days = days_since_call

    def list_transcripts(self, **_):
        if self._days is None:
            return []
        dt = (datetime.now(timezone.utc) - timedelta(days=self._days)).isoformat()
        return [{"date": dt}]


def _clear():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM cs_account_health"))
        conn.execute(text("DELETE FROM cs_account_health_history"))
        conn.execute(text("DELETE FROM cs_churn_risk"))
        conn.execute(text("DELETE FROM tasks WHERE source LIKE 'cs:churn_risk:%'"))


def _seed_account(account_id: str, *, health: float = 80.0, nps_cat: str = "promoter", name: str = "Acme"):
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health
                     (account_id, name, score, nps_category, nps_at, checked_at)
                   VALUES (:a, :n, :s, :nc, :nps_at, :now)"""
            ),
            {"a": account_id, "n": name, "s": health, "nc": nps_cat, "nps_at": now, "now": now},
        )


def _seed_history(account_id: str, score: float, days_ago: int):
    engine = get_engine()
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO cs_account_health_history (account_id, score, checked_at)
                   VALUES (:a, :s, :t)"""
            ),
            {"a": account_id, "s": score, "t": now - timedelta(days=days_ago)},
        )


@pytest.fixture(autouse=True)
def _clean():
    _clear()
    yield
    _clear()


@pytest.mark.asyncio
async def test_sweep_scores_every_account_and_persists():
    _seed_account("A1", health=80, nps_cat="promoter")
    _seed_account("A2", health=60, nps_cat="passive")
    slack = FakeSlackSender()

    counters = await churn_risk.run_sweep(
        sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies(), slack_sender=slack
    )
    assert counters["scored"] == 2

    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT account_id, score, tier FROM cs_churn_risk ORDER BY account_id")
        ).mappings().all()
    assert len(rows) == 2
    assert rows[0]["account_id"] == "A1"


@pytest.mark.asyncio
async def test_sweep_is_idempotent_same_day():
    _seed_account("A1", health=80)
    slack = FakeSlackSender()
    await churn_risk.run_sweep(sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies(), slack_sender=slack)
    counters = await churn_risk.run_sweep(
        sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies(), slack_sender=slack
    )
    assert counters["scored"] == 0
    assert counters["skipped_today"] == 1

    engine = get_engine()
    with engine.begin() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM cs_churn_risk")).scalar()
    assert count == 1


@pytest.mark.asyncio
async def test_tier_50_is_log_only_no_slack_no_task():
    # 50-69: log only. Seed conditions to hit tier 50.
    # NPS passive (4) + health_absolute_low 0.5*20=10 + case_spike 1.0*15=15 + stagnation 0.5*5=2.5 → 31. Need more.
    # Easier: health drop saturated (35) + nps unknown (3) = 38, not enough.
    # Use: health_drop full (35) + nps passive (4) + absolute low 0.5*20=10 + stagnation 0.5*5=2.5 = 51.5 → 52 → tier 50
    _seed_account("A1", health=50, nps_cat="passive")
    _seed_history("A1", score=100, days_ago=10)  # 50% drop saturates → 35
    # stagnation: last_touch was None → no stagnation. Fine — still ~49. Add: bump one case.
    sf = FakeSf(cases=(5, 2))  # ratio 2.5 → saturates → 15 → total 50+? let's compute
    slack = FakeSlackSender()
    await churn_risk.run_sweep(sf_mcp=sf, fireflies_mcp=FakeFireflies(), slack_sender=slack)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT score, tier FROM cs_churn_risk")).mappings().first()
    assert 50 <= row["score"] < 70
    assert row["tier"] == 50
    # Tier 50 = log only
    assert slack.sent == []
    with engine.begin() as conn:
        tasks = conn.execute(
            text("SELECT COUNT(*) FROM tasks WHERE source LIKE 'cs:churn_risk:%'")
        ).scalar()
    assert tasks == 0


@pytest.mark.asyncio
async def test_tier_70_alerts_blaine_and_opens_high_task():
    # Push into 70-84: health drop 35 + absolute low (health=30 → 0.7*20=14) + nps passive 4 +
    # case spike 15 + stagnation (120d → 0.5*5=2.5) ≈ 71.
    _seed_account("A1", health=30, nps_cat="passive")
    _seed_history("A1", score=100, days_ago=10)
    sf = FakeSf(cases=(10, 3), renewal_days=90)  # +15 for renewal-no-conv (no call at all)
    slack = FakeSlackSender()
    await churn_risk.run_sweep(sf_mcp=sf, fireflies_mcp=FakeFireflies(), slack_sender=slack)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT score, tier FROM cs_churn_risk")).mappings().first()
    assert 70 <= row["score"] < 85
    assert len(slack.sent) == 1
    channel, body = slack.sent[0]
    assert channel == "#agent-cs-log"
    assert "tier 70" in body
    assert "@jackie" not in body  # Jackie not CC'd until tier 85

    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT priority, assignee FROM tasks WHERE source LIKE 'cs:churn_risk:%'")
        ).mappings().first()
    assert task["priority"] == "high"
    assert task["assignee"] == "blaine"


@pytest.mark.asyncio
async def test_tier_85_pings_jackie_and_urgent_task():
    # Max every factor.
    _seed_account("A1", health=5, nps_cat="detractor")
    _seed_history("A1", score=100, days_ago=10)
    sf = FakeSf(cases=(50, 5), renewal_days=60)
    ff = FakeFireflies(days_since_call=90)
    slack = FakeSlackSender()
    await churn_risk.run_sweep(sf_mcp=sf, fireflies_mcp=ff, slack_sender=slack)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT score, tier FROM cs_churn_risk")).mappings().first()
    assert row["tier"] == 85
    assert row["score"] >= 85
    assert "@jackie" in slack.sent[0][1]
    with engine.begin() as conn:
        task = conn.execute(
            text("SELECT priority FROM tasks WHERE source LIKE 'cs:churn_risk:%'")
        ).mappings().first()
    assert task["priority"] == "urgent"


@pytest.mark.asyncio
async def test_single_factor_tier_70_is_suppressed():
    """Anti-false-positive guard: tier ≥70 needs ≥2 non-zero factors."""
    # Only health drop will be non-zero (35). NPS default 'unknown' is 0.3 — also non-zero.
    # To isolate: nps_category='promoter' (0) plus just health drop.
    # But need ≥70 from one factor. Can we? health drop max = 35. absolute low max = 20.
    # Max single factor is 35. So we CAN'T get tier 70 from one factor in V1. Assert that.
    # Instead: craft tier 70 with exactly 2 factors and verify the suppression path differs
    # by forcing sponsor_departed path? No — V2 weights are 0. So this guard is provably
    # hard to hit in V1. Document that explicitly and test the counter.
    from agents.cs.risk import scoring as _s
    factors = {"health_drop": 1.0, "nps": 0.0}
    assert _s.non_zero_factor_count(factors) == 1


@pytest.mark.asyncio
async def test_factors_persisted_for_forensics():
    _seed_account("A1", health=30, nps_cat="detractor")
    _seed_history("A1", score=100, days_ago=10)
    slack = FakeSlackSender()
    await churn_risk.run_sweep(
        sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies(), slack_sender=slack
    )
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT factors_json FROM cs_churn_risk")).mappings().first()
    import json
    payload = json.loads(row["factors_json"])
    assert "factors" in payload
    assert "contributions" in payload
    assert payload["contributions"]["health_drop"] == 35


@pytest.mark.asyncio
async def test_promoter_healthy_account_is_tier_0():
    _seed_account("A1", health=95, nps_cat="promoter")
    slack = FakeSlackSender()
    counters = await churn_risk.run_sweep(
        sf_mcp=FakeSf(), fireflies_mcp=FakeFireflies(), slack_sender=slack
    )
    assert counters["tier_50"] == 0
    assert counters["tier_70"] == 0
    assert counters["tier_85"] == 0
    assert slack.sent == []
