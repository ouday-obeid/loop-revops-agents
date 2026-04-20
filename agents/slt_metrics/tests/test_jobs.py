"""Scheduled-job callables — wiring between fetcher and snapshotter.

Real SF + DB integration is out of scope here (that's the D15 dry-run); these
tests prove the chain calls the right pieces with the right arguments.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import text

from agents.slt_metrics import jobs
from agents.slt_metrics.pipeline import fetcher, snapshotter
from agents.slt_metrics.types import ContactRole, OppRecord
from shared.db.connection import get_engine


@pytest.fixture(autouse=True)
def _clean_snapshots():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pipeline_snapshots"))
    yield


@pytest.fixture(autouse=True)
def _stub_briefing_fetchers(monkeypatch):
    """`_build_payload` calls closed + all-opps fetchers; default them to empty
    so briefing tests stay DB-driven. Individual tests can override."""
    monkeypatch.setattr(fetcher, "fetch_closed_opps_quarter", lambda **_: [])
    monkeypatch.setattr(fetcher, "fetch_all_opps_snapshot", lambda **_: [])


def _opp(**overrides: Any) -> OppRecord:
    base = dict(
        id="0061x100",
        name="Test Opp",
        account_id="001ACC",
        account_name="Test Acct",
        account_website=None,
        account_type=None,
        owner_id="005OWN",
        owner_name="Nate",
        owner_role=None,
        owner_manager=None,
        stage="Proposal",
        is_closed=False,
        is_won=False,
        amount=120000.0,
        acv=90000.0,
        fixed_arr=None,
        locations=10,
        type=None,
        lead_source=None,
        close_date=date(2026, 6, 30),
        created_date=None,
        last_activity_date=None,
        last_modified_date=None,
        last_stage_change_date=None,
        days_since_stage_change=None,
        time_in_stage=None,
        probability_sf=None,
        description=None,
        next_steps=None,
        next_step_date=None,
        icp_score=None,
        segment="MM",
        products={},
        contact_roles=[],
        raw={"Id": "0061x100"},
    )
    base.update(overrides)
    return OppRecord(**base)


def test_run_morning_snapshot_writes_today(monkeypatch):
    opps = [_opp(id="0061x100"), _opp(id="0061x200", acv=250000.0)]
    monkeypatch.setattr(fetcher, "fetch_open_opps", lambda: opps)

    result = jobs.run_morning_snapshot()
    assert result == {"fetched": 2, "inserted": 2}

    rows = snapshotter.read_snapshot(date.today())
    assert len(rows) == 2
    # Unscored rows: score/category/probability/weighted_acv all NULL.
    for row in rows:
        assert row["score"] is None
        assert row["category"] is None
        assert row["probability"] is None
        assert row["weighted_acv"] is None


def test_run_morning_snapshot_idempotent_on_rerun(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_open_opps", lambda: [_opp()])
    first = jobs.run_morning_snapshot()
    second = jobs.run_morning_snapshot()
    assert first["inserted"] == 1
    assert second["inserted"] == 0  # ON CONFLICT DO NOTHING


def test_run_morning_snapshot_empty_fetch(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_open_opps", lambda: [])
    assert jobs.run_morning_snapshot() == {"fetched": 0, "inserted": 0}


def test_schedule_registers_slt_morning_snapshot():
    from shared.runtime.schedule import by_name

    job = by_name("slt-morning-snapshot")
    assert job is not None
    assert job.cron == "30 6 * * 1-5"
    assert job.callable_path == "agents.slt_metrics.jobs:run_morning_snapshot"


def test_schedule_registers_slt_daily_and_friday():
    from shared.runtime.schedule import by_name

    daily = by_name("slt-daily-briefing")
    friday = by_name("slt-friday-review")
    assert daily is not None and daily.cron == "0 8 * * 1-5"
    assert daily.callable_path == "agents.slt_metrics.jobs:run_daily_briefing"
    assert friday is not None and friday.cron == "30 15 * * 5"
    assert friday.callable_path == "agents.slt_metrics.jobs:run_friday_review"


# ---------------------------------------------------------------- briefing helpers

class _CapturingSender:
    """Fake `SenderFn` that records every send call."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, channel: str, text_: str, blocks: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        self.calls.append({"channel": channel, "text": text_, "blocks": blocks})
        return {"ok": True, "ts": "1.0", "channel": channel}


class _StubRouter:
    """Stand-in ClaudeRouter that returns the fallback text deterministically."""

    def narrate(
        self, kind: str, *, system: str, user: str, fallback: str,
        max_tokens: int | None = None,
    ) -> str:
        return fallback


def _seed_scored_snapshot(
    *, snapshot_date: date, opp_id: str = "0061ABC", acv: float = 80000.0,
    score: int = 85, stage: str = "Proposal", owner: str = "Nate",
) -> None:
    """Insert a scored snapshot row directly — bypasses the morning fetch/score."""
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO pipeline_snapshots
                    (snapshot_date, opp_id, stage, amount, acv, close_date,
                     owner_id, owner_name, account_id, segment,
                     score, category, probability, weighted_acv, metadata)
                VALUES
                    (:d, :id, :st, :am, :acv, :cd,
                     '005', :own, '001', 'MM',
                     :sc, 'Strong Commit', 0.8, :wacv,
                     '{"pillars": {}, "risk_flags": [], "weights_version": "v1-seed", "sf_raw": {"Name": "Seed Opp", "Account": {"Name": "Seed Acct"}}}')
                """
            ),
            {
                "d": snapshot_date, "id": opp_id, "st": stage, "am": acv * 1.2,
                "acv": acv, "cd": date(2026, 9, 30), "own": owner,
                "sc": score, "wacv": acv * 0.8,
            },
        )


def test_run_daily_briefing_no_data_returns_status():
    result = jobs.run_daily_briefing(today=date(2026, 4, 13))
    assert result == {"status": "no_data", "run_date": "2026-04-13", "deals": 0}


def test_run_daily_briefing_sends_draft_and_creates_gate():
    today = date(2026, 4, 13)
    _seed_scored_snapshot(snapshot_date=today, opp_id="0061AA", acv=95000.0, score=85)
    _seed_scored_snapshot(snapshot_date=today, opp_id="0061BB", acv=120000.0, score=60)

    cap = _CapturingSender()
    result = jobs.run_daily_briefing(today=today, sender=cap, router=_StubRouter())

    assert result["status"] == "sent"
    assert result["kind"] == "daily"
    assert result["deals"] == 2
    assert isinstance(result["gate_id"], int) and result["gate_id"] > 0
    assert result["slack_ok"] is True

    # Sent to O's DM.
    assert len(cap.calls) == 1
    assert cap.calls[0]["channel"] == jobs._O_DM_CHANNEL
    # Approval header block leads the message.
    blocks = cap.calls[0]["blocks"]
    assert blocks[0]["type"] == "header"
    assert "Approval needed" in blocks[0]["text"]["text"]

    # Gate row exists with the right action_type.
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT action_type, status, payload FROM approval_gates WHERE id = :id"),
            {"id": result["gate_id"]},
        ).fetchone()
    assert row[0] == "slt_draft_review"
    assert row[1] == "pending"
    assert '"kind": "daily"' in row[2]


def test_run_daily_briefing_diffs_against_prior_snapshot():
    today = date(2026, 4, 13)
    yesterday = date(2026, 4, 12)
    _seed_scored_snapshot(snapshot_date=yesterday, opp_id="0061MOVE", acv=60000.0, score=60, stage="Business Case")
    _seed_scored_snapshot(snapshot_date=today,     opp_id="0061MOVE", acv=60000.0, score=85, stage="Proposal")
    _seed_scored_snapshot(snapshot_date=today,     opp_id="0061NEW",  acv=100000.0, score=85)

    cap = _CapturingSender()
    result = jobs.run_daily_briefing(today=today, sender=cap, router=_StubRouter())

    assert result["prev_date"] == "2026-04-12"
    # One advance + one new deal.
    assert result["movers"] >= 2


def test_run_daily_briefing_falls_back_to_latest_snapshot():
    # Today has nothing; the cron should still publish yesterday's briefing.
    today = date(2026, 4, 13)
    yesterday = date(2026, 4, 12)
    _seed_scored_snapshot(snapshot_date=yesterday, opp_id="0061YEST")

    cap = _CapturingSender()
    result = jobs.run_daily_briefing(today=today, sender=cap, router=_StubRouter())
    assert result["status"] == "sent"
    assert result["run_date"] == "2026-04-12"


def test_run_friday_review_sends_draft():
    today = date(2026, 4, 17)  # Friday
    _seed_scored_snapshot(snapshot_date=today, opp_id="0061FR", acv=200000.0, score=85)

    cap = _CapturingSender()
    result = jobs.run_friday_review(today=today, sender=cap, router=_StubRouter())
    assert result["status"] == "sent"
    assert result["kind"] == "friday"
    assert len(cap.calls) == 1


def test_run_friday_review_seven_day_lookback_ignores_older_prev():
    today = date(2026, 4, 17)
    # 10-day-old prev is outside the 7-day friday window — no diff baseline.
    _seed_scored_snapshot(snapshot_date=date(2026, 4, 7), opp_id="0061STALE")
    _seed_scored_snapshot(snapshot_date=today, opp_id="0061FR")

    cap = _CapturingSender()
    result = jobs.run_friday_review(today=today, sender=cap, router=_StubRouter())
    # With no baseline inside the window, today's row renders as a "new" mover.
    assert result["prev_date"] is None or date.fromisoformat(result["prev_date"]) >= today - timedelta(days=7)


def test_row_to_scored_deal_rehydrates_metadata():
    today = date(2026, 4, 13)
    _seed_scored_snapshot(snapshot_date=today, opp_id="0061HYD", acv=50000.0, score=72)
    rows = snapshotter.read_snapshot(today)
    deal = jobs._row_to_scored_deal(rows[0])
    assert deal.opp_name == "Seed Opp"
    assert deal.account_name == "Seed Acct"
    assert deal.score == 72
    assert deal.weights_version == "v1-seed"


def test_horizon_quarter_computes_fiscal_label():
    assert jobs._horizon_quarter(date(2026, 1, 15)) == "FY2026-Q1"
    assert jobs._horizon_quarter(date(2026, 4, 13)) == "FY2026-Q2"
    assert jobs._horizon_quarter(date(2026, 7, 1)) == "FY2026-Q3"
    assert jobs._horizon_quarter(date(2026, 12, 31)) == "FY2026-Q4"


# ------------------------------------------------------------------ payload extensions

def test_build_payload_populates_closed_and_all_opps(monkeypatch):
    """_build_payload threads both new fetchers into the payload."""
    closed = [_opp(id="0061CLOSED", stage="Closed Won", is_closed=True, is_won=True)]
    all_opps = [
        _opp(id="0061OPEN1"),
        _opp(id="0061CLOSED", stage="Closed Won", is_closed=True, is_won=True),
    ]
    monkeypatch.setattr(fetcher, "fetch_closed_opps_quarter", lambda **_: closed)
    monkeypatch.setattr(fetcher, "fetch_all_opps_snapshot", lambda **_: all_opps)

    today = date(2026, 4, 13)
    _seed_scored_snapshot(snapshot_date=today, opp_id="0061AA")

    cap = _CapturingSender()
    payload_holder: dict[str, Any] = {}

    def spy_compose(payload, **kwargs):
        payload_holder["payload"] = payload
        return {"text": "stub", "blocks": []}

    monkeypatch.setattr(jobs, "compose_daily", spy_compose)
    jobs.run_daily_briefing(today=today, sender=cap, router=_StubRouter())

    payload = payload_holder["payload"]
    assert [o.id for o in payload.closed_opps_quarter] == ["0061CLOSED"]
    assert {o.id for o in payload.all_opps_snapshot} == {"0061OPEN1", "0061CLOSED"}


def test_run_morning_snapshot_does_not_call_new_fetchers(monkeypatch):
    """Morning snapshot writes open opps only; the closed + all-opps fetchers
    are briefing-time concerns."""
    monkeypatch.setattr(fetcher, "fetch_open_opps", lambda: [_opp()])

    def _boom(**_):
        raise AssertionError("morning snapshot must not call this fetcher")

    monkeypatch.setattr(fetcher, "fetch_closed_opps_quarter", _boom)
    monkeypatch.setattr(fetcher, "fetch_all_opps_snapshot", _boom)

    assert jobs.run_morning_snapshot() == {"fetched": 1, "inserted": 1}
