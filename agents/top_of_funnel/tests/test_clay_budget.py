"""D4 DoD tests for Clay credit budget + enrichment gating.

Covers:
  * 80% threshold alert fires exactly once per month
  * 100% threshold raises ClayBudgetExceeded (with rollback — consumed never > cap)
  * grade-below-floor returns skipped result, spends zero credits
  * month rollover resets the ledger independently
  * credit_status shape + progress-bar rendering
  * from_env / invalid-cap edge cases
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from agents.top_of_funnel.enrichment import clay_client
from agents.top_of_funnel.enrichment.clay_client import (
    ClayBudgetExceeded,
    ClayEnrichResult,
    CreditBudget,
    credit_status,
    enrich_contact,
)
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset_ledger():
    """Every test starts with an empty credit ledger."""
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM clay_credit_ledger"))
    yield


def _collect_alerts() -> tuple[list[str], callable]:
    """Helper: returns (messages_list, callback_to_inject)."""
    messages: list[str] = []
    return messages, messages.append


# ---------------------------------------------------------------- construction


def test_negative_cap_rejected():
    with pytest.raises(ValueError):
        CreditBudget(monthly_cap=0)
    with pytest.raises(ValueError):
        CreditBudget(monthly_cap=-10)


def test_from_env_reads_cap(monkeypatch):
    monkeypatch.setenv("CLAY_MONTHLY_BUDGET_CREDITS", "7500")
    b = CreditBudget.from_env()
    assert b.monthly_cap == 7500


def test_from_env_rejects_nonint(monkeypatch):
    monkeypatch.setenv("CLAY_MONTHLY_BUDGET_CREDITS", "five-thousand")
    with pytest.raises(ValueError):
        CreditBudget.from_env()


def test_from_env_default(monkeypatch):
    monkeypatch.delenv("CLAY_MONTHLY_BUDGET_CREDITS", raising=False)
    assert CreditBudget.from_env().monthly_cap == 50_000


# --------------------------------------------------------------------- usage


def test_usage_starts_at_zero():
    b = CreditBudget(100)
    assert b.usage() == 0
    assert b.remaining() == 100
    assert b.usage_pct() == 0.0


def test_spend_accumulates():
    b = CreditBudget(100)
    assert b.spend(10) == 10
    assert b.spend(15) == 25
    assert b.usage() == 25
    assert b.remaining() == 75
    assert b.usage_pct() == pytest.approx(0.25)


def test_spend_negative_rejected():
    b = CreditBudget(100)
    with pytest.raises(ValueError):
        b.spend(-1)


# ------------------------------------------------------------- 80% threshold


def test_80pct_alert_fires_once():
    """Crossing 80% once fires one alert; a second spend in the same month
    must NOT re-alert for 80% (the ledger records alerted_80pct_at)."""
    messages, cb = _collect_alerts()
    b = CreditBudget(1000, alert_callback=cb)

    b.spend(700)
    assert messages == []  # still below 80%
    b.spend(100)  # now at 800 = exactly 80%
    assert len(messages) == 1
    assert "80%" in messages[0]

    # Another spend in the same month — no second 80% alert.
    b.spend(50)
    assert len(messages) == 1


def test_80pct_alert_on_cross_not_exact():
    """Crossing 80% via a large jump (e.g. 500 → 900) still fires once."""
    messages, cb = _collect_alerts()
    b = CreditBudget(1000, alert_callback=cb)

    b.spend(500)
    b.spend(400)  # jumps from 50% straight to 90%
    assert len(messages) == 1
    assert "80%" in messages[0]


# ------------------------------------------------------------- 100% threshold


def test_100pct_exact_fires_alert_without_raising():
    """Spending up to EXACTLY the cap is allowed. One 100% alert fires."""
    messages, cb = _collect_alerts()
    b = CreditBudget(100, alert_callback=cb)

    b.spend(100)
    # We crossed 80% and hit 100% in the same call — two alerts expected.
    assert any("80%" in m for m in messages)
    assert any("100%" in m for m in messages)
    assert b.usage() == 100
    assert b.remaining() == 0


def test_over_cap_raises_and_rolls_back():
    """Spending past the cap raises ClayBudgetExceeded; consumed must NOT
    be incremented (no partial spend). This is the hard-block contract."""
    messages, cb = _collect_alerts()
    b = CreditBudget(100, alert_callback=cb)

    b.spend(90)
    with pytest.raises(ClayBudgetExceeded):
        b.spend(20)  # would go to 110

    # consumed stayed at 90 — the rejected 20 was NOT committed
    assert b.usage() == 90
    assert any("100%" in m for m in messages)


def test_over_cap_second_attempt_does_not_realert():
    """After the ledger is marked 100%, subsequent over-cap attempts still
    raise but MUST NOT spam Slack with duplicate 100% alerts."""
    messages, cb = _collect_alerts()
    b = CreditBudget(100, alert_callback=cb)

    b.spend(90)
    with pytest.raises(ClayBudgetExceeded):
        b.spend(20)
    first_count = sum(1 for m in messages if "100%" in m)

    with pytest.raises(ClayBudgetExceeded):
        b.spend(50)
    second_count = sum(1 for m in messages if "100%" in m)

    assert second_count == first_count  # no new 100% alert


# ---------------------------------------------------------------- month roll


def test_month_rollover_isolated():
    """Two months have independent counters + independent alert state."""
    messages, cb = _collect_alerts()
    b = CreditBudget(100, alert_callback=cb)

    b.spend(50, month="2026-04")
    b.spend(90, month="2026-05")
    assert b.usage(month="2026-04") == 50
    assert b.usage(month="2026-05") == 90

    # April at 50% → no alert; May at 90% → 80% alert already sent.
    april_alerts = [m for m in messages if "2026-04" in m]
    may_alerts = [m for m in messages if "2026-05" in m]
    assert april_alerts == []
    assert any("80%" in m for m in may_alerts)


# ---------------------------------------------------------- grade-floor gate


@pytest.mark.asyncio
async def test_grade_below_floor_skips_without_spend():
    b = CreditBudget(100)
    result = await enrich_contact(
        domain="example.com",
        grade="C",
        grade_floor="B",
        budget=b,
    )
    assert isinstance(result, ClayEnrichResult)
    assert result.skipped is True
    assert "grade_below_floor" in result.skip_reason
    assert result.credits_used == 0
    assert b.usage() == 0  # no spend


@pytest.mark.asyncio
async def test_grade_unavailable_skips():
    b = CreditBudget(100)
    result = await enrich_contact(domain="x.com", grade="unavailable", budget=b)
    assert result.skipped is True
    assert b.usage() == 0


@pytest.mark.asyncio
async def test_grade_at_floor_spends():
    b = CreditBudget(100)
    result = await enrich_contact(
        domain="example.com",
        grade="B",
        grade_floor="B",
        budget=b,
    )
    assert result.skipped is False
    assert result.credits_used == 1
    assert b.usage() == 1


@pytest.mark.asyncio
async def test_grade_above_floor_spends():
    b = CreditBudget(100)
    result = await enrich_contact(
        domain="example.com",
        grade="A",
        grade_floor="B",
        budget=b,
    )
    assert result.skipped is False
    assert result.credits_used == 1


@pytest.mark.asyncio
async def test_enrich_at_100pct_hard_blocks():
    """Once the ledger is 100% consumed, enrich_contact raises — even for an A."""
    b = CreditBudget(5)
    for _ in range(5):
        await enrich_contact(domain="x.com", grade="A", budget=b)
    assert b.usage() == 5

    with pytest.raises(ClayBudgetExceeded):
        await enrich_contact(domain="x.com", grade="A", budget=b)


# --------------------------------------------------------------- credit_status


@pytest.mark.asyncio
async def test_credit_status_shape(monkeypatch):
    monkeypatch.setenv("CLAY_MONTHLY_BUDGET_CREDITS", "1000")
    # Burn 250 credits so the bar has visible fill.
    b = CreditBudget.from_env()
    b.spend(250)

    out = await credit_status()
    assert set(out) >= {"text", "used", "cap", "remaining", "pct"}
    assert out["cap"] == 1000
    assert out["used"] == 250
    assert out["remaining"] == 750
    assert out["pct"] == pytest.approx(25.0)
    # Progress bar: ~5 filled / 20
    assert "`" in out["text"]
    assert "Clay credits" in out["text"]


# ------------------------------------------------------------- persistence


def test_ledger_row_persists_across_instances():
    """CreditBudget is stateless — two instances sharing the same DB see the
    same consumed counter for the same month."""
    messages, cb = _collect_alerts()
    a = CreditBudget(100, alert_callback=cb)
    a.spend(40)

    b = CreditBudget(100, alert_callback=cb)
    assert b.usage() == 40

    b.spend(50)
    assert a.usage() == 90  # first instance sees it too


def test_month_key_format():
    key = CreditBudget._month_key(datetime(2026, 4, 13, tzinfo=timezone.utc))
    assert key == "2026-04"
