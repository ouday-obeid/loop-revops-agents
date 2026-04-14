"""Narrator budget routing + daily/Friday briefing composition."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from agents.slt_metrics.briefings import daily_830, friday_review, narrator
from agents.slt_metrics.briefings.narrator import (
    BUDGET_DOWNSHIFT_PCT,
    MODEL_OPUS,
    MODEL_SONNET,
    ClaudeRouter,
)
from agents.slt_metrics.types import (
    AeCard,
    BoardMetrics,
    ForecastRollup,
    ForecastWeights,
    MoverSet,
    Mover,
    PillarScore,
    RevenueModelPayload,
    ScoredDeal,
    SdrCard,
    UnitEconomics,
)
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _wipe_agent_runs():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM agent_runs WHERE agent_name = 'slt_metrics_router_test'"))
    yield


def _insert_tokens(agent_name: str, *, tokens: int, started_at: datetime):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_runs (agent_name, trigger, input, status, started_at, tokens_used) "
                "VALUES (:a, 'test', '{}', 'completed', :s, :t)"
            ),
            {"a": agent_name, "s": started_at, "t": tokens},
        )


# ------------------------------------------------------------------ narrator

def test_select_model_returns_routed_model_when_under_budget(monkeypatch):
    router = ClaudeRouter(agent_name="slt_metrics_router_test", monthly_budget=1_000_000)
    monkeypatch.setattr(router, "month_to_date_tokens", lambda: 100_000)
    assert router.select_model("daily_briefing") == MODEL_SONNET
    assert router.select_model("mover_wrap") == MODEL_OPUS
    assert router.select_model("friday_wrap") == MODEL_OPUS


def test_select_model_downshifts_opus_above_threshold(monkeypatch):
    router = ClaudeRouter(agent_name="slt_metrics_router_test", monthly_budget=1_000_000)
    # 91% of budget → Opus kinds must downshift.
    monkeypatch.setattr(router, "month_to_date_tokens", lambda: int(0.91 * 1_000_000))
    assert router.select_model("mover_wrap") == MODEL_SONNET
    assert router.select_model("friday_wrap") == MODEL_SONNET
    # Sonnet kinds untouched.
    assert router.select_model("daily_briefing") == MODEL_SONNET


def test_select_model_rejects_unknown_kind():
    router = ClaudeRouter(agent_name="slt_metrics_router_test")
    with pytest.raises(ValueError, match="Unknown narrative kind"):
        router.select_model("mystery_genre")


def test_narrate_returns_fallback_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    router = ClaudeRouter(agent_name="slt_metrics_router_test")
    out = router.narrate(
        "daily_briefing", system="sys", user="user",
        fallback="FALLBACK-TEXT",
    )
    assert out == "FALLBACK-TEXT"


def test_narrate_uses_injected_client_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")

    class _FakeResp:
        def __init__(self, text_):
            self.content = [type("Block", (), {"text": text_})()]

    class _FakeMessages:
        def __init__(self):
            self.calls = []

        def create(self, *, model, max_tokens, system, messages):
            self.calls.append((model, max_tokens))
            return _FakeResp("hello narrative")

    class _FakeClient:
        def __init__(self):
            self.messages = _FakeMessages()

    fake = _FakeClient()
    router = ClaudeRouter(
        agent_name="slt_metrics_router_test",
        client_factory=lambda: fake,
    )
    out = router.narrate(
        "daily_briefing", system="sys", user="user",
        fallback="should-not-be-returned",
    )
    assert out == "hello narrative"
    assert fake.messages.calls[0][0] == MODEL_SONNET


def test_narrate_falls_back_on_client_exception(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")

    class _Boom:
        class messages:
            @staticmethod
            def create(**_):
                raise RuntimeError("quota exceeded")

    router = ClaudeRouter(
        agent_name="slt_metrics_router_test",
        client_factory=lambda: _Boom(),
    )
    out = router.narrate(
        "daily_briefing", system="sys", user="user",
        fallback="SAFE-FALLBACK",
    )
    assert out == "SAFE-FALLBACK"


def test_month_to_date_tokens_sums_only_current_month():
    agent = "slt_metrics_router_test"
    now = datetime.now(timezone.utc)
    this_month = datetime(now.year, now.month, 10, tzinfo=timezone.utc)
    last_month = datetime(now.year - 1, 12, 15, tzinfo=timezone.utc) if now.month == 1 \
        else datetime(now.year, now.month - 1, 15, tzinfo=timezone.utc)
    _insert_tokens(agent, tokens=1_000, started_at=this_month)
    _insert_tokens(agent, tokens=9_999_999, started_at=last_month)  # should NOT be counted

    router = ClaudeRouter(agent_name=agent, monthly_budget=1_000_000)
    assert router.month_to_date_tokens() == 1_000


# ------------------------------------------------------------------ fixtures for briefings

def _pillars() -> dict[str, PillarScore]:
    return {
        "icp":      PillarScore(0.8, "sf-icp-score"),
        "stage":    PillarScore(0.7, "Pilot"),
        "activity": PillarScore(0.6, "recent"),
        "timeline": PillarScore(0.65, "in-60d"),
        "call":     PillarScore(0.5, "1-transcript"),
    }


def _payload(*, flagged_deal: bool = True) -> RevenueModelPayload:
    scored = [
        ScoredDeal(
            opp_id="A1", opp_name="Opp A1", owner_name="Jane", account_name="Acme",
            segment="ENT", stage="Pilot", amount=400_000, acv=400_000, close_date=date(2026, 5, 15),
            score=85, probability=0.85, category="Strong Commit", weighted_acv=340_000,
            pillars=_pillars(), risk_flags=["STAGE_MISMATCH"] if flagged_deal else [],
            weights_version="v1-seed",
        ),
        ScoredDeal(
            opp_id="B2", opp_name="Opp B2", owner_name="Jim", account_name="Beta",
            segment="MM", stage="Discovery", amount=100_000, acv=100_000, close_date=date(2026, 6, 1),
            score=55, probability=0.55, category="Commit", weighted_acv=55_000,
            pillars=_pillars(), risk_flags=[], weights_version="v1-seed",
        ),
    ]
    movers = MoverSet(
        period_from=date(2026, 4, 12), period_to=date(2026, 4, 13),
        movers=[
            Mover(
                opp_id="A1", opp_name="Opp A1", owner_name="Jane", kind="advanced",
                before={"stage": "Discovery"}, after={"stage": "Pilot"},
                delta_acv=50_000, delta_days=None,
            ),
        ],
    )
    rollup = ForecastRollup(
        horizon_quarter="FY2026-Q2",
        commit_amount=340_000, best_case_amount=395_000, weighted_amount=300_000,
        deal_count=2,
        by_owner={"Jane": {"commit": 340_000, "best_case": 340_000, "weighted": 290_000}},
        by_segment={"ENT": {"commit": 340_000, "best_case": 340_000, "weighted": 290_000}},
    )
    ae_cards = [
        AeCard(
            rep_email="jane@tryloop.ai", rep_name="Jane",
            attainment_pct=0.6, close_rate_pct=0.4, avg_cycle_days=45.0, avg_acv=200_000.0,
            pipeline_created=500_000, pipeline_advanced=220_000,
            call_grade_avg=0.72, rep_perf_score=80, deals_open=3, deals_commit=1,
        ),
    ]
    sdr_cards = [
        SdrCard(
            sdr_email="sam@tryloop.ai", sdr_name="Sam",
            dials=120, connects=30, meetings_set=12, meetings_held=8,
            pipeline_sourced=340_000.0, pipeline_advanced=90_000.0, leaderboard_rank=1,
        ),
    ]
    bm = BoardMetrics(
        as_of=date(2026, 4, 13),
        arr=14_000_000, nrr=1.12, logo_retention=0.92, expansion_rate=0.20,
        pipeline_coverage_mm=3.2, pipeline_coverage_ent=4.5,
        unit_economics=UnitEconomics(
            gross_revenue_retention=0.95, net_revenue_retention=1.12,
            logo_retention=0.92, expansion_rate=0.20, cac_payback_months=14.0,
            ltv_cac_ratio=3.8, gap_flag=False,
        ),
    )
    return RevenueModelPayload(
        run_date=date(2026, 4, 13), horizon_quarter="FY2026-Q2",
        weights=ForecastWeights(),
        scored_deals=scored, forecast_rollup=rollup,
        movers=movers, ae_cards=ae_cards, sdr_cards=sdr_cards,
        board_metrics=bm,
    )


class _StubRouter:
    def narrate(self, kind, *, system, user, fallback, max_tokens=None):
        return f"[{kind}] ok"


# ------------------------------------------------------------------ daily

def test_compose_daily_returns_text_and_blocks():
    out = daily_830.compose_daily(_payload(), router=_StubRouter())
    assert "SLT briefing" in out["text"]
    assert isinstance(out["blocks"], list) and len(out["blocks"]) >= 6


def test_compose_daily_includes_headline_movers_risks_coverage():
    out = daily_830.compose_daily(_payload(), router=_StubRouter())
    rendered = "\n".join(
        b.get("text", {}).get("text", "") for b in out["blocks"] if b["type"] == "section"
    )
    assert "Commit" in rendered
    assert "Top movers" in rendered
    assert "STAGE_MISMATCH" in rendered       # flagged deal made the risk watch
    assert "Pipeline coverage" in rendered
    assert "[daily_briefing] ok" in rendered  # narrator output threaded through


def test_compose_daily_no_flagged_deals_renders_empty_risk_banner():
    out = daily_830.compose_daily(_payload(flagged_deal=False), router=_StubRouter())
    rendered = "\n".join(
        b.get("text", {}).get("text", "") for b in out["blocks"] if b["type"] == "section"
    )
    assert "No flagged risks" in rendered


def test_compose_daily_includes_workbook_link_when_url_given():
    out = daily_830.compose_daily(
        _payload(), router=_StubRouter(), workbook_url="https://drive.example/abc",
    )
    ctx_blocks = [b for b in out["blocks"] if b["type"] == "context"]
    assert ctx_blocks, "expected workbook context block"
    assert "drive.example/abc" in ctx_blocks[0]["elements"][0]["text"]


# ------------------------------------------------------------------ friday

def test_compose_friday_returns_text_and_blocks():
    out = friday_review.compose_friday(_payload(), router=_StubRouter())
    assert "Friday review" in out["text"]
    assert isinstance(out["blocks"], list)


def test_compose_friday_surfaces_top_performers_and_coverage():
    out = friday_review.compose_friday(_payload(), router=_StubRouter())
    rendered = "\n".join(
        b.get("text", {}).get("text", "") for b in out["blocks"] if b["type"] == "section"
    )
    assert "Top AE" in rendered and "Jane" in rendered
    assert "Top SDR" in rendered and "Sam" in rendered
    assert "Weekly movers" in rendered
    assert "Coverage" in rendered
    assert "[friday_wrap] ok" in rendered


def test_compose_friday_handles_empty_cards_without_raising():
    payload = _payload()
    payload.ae_cards.clear()
    payload.sdr_cards.clear()
    out = friday_review.compose_friday(payload, router=_StubRouter())
    rendered = "\n".join(
        b.get("text", {}).get("text", "") for b in out["blocks"] if b["type"] == "section"
    )
    assert "Top AE:* —" in rendered
    assert "Top SDR:* —" in rendered
