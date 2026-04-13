"""D7 tests for daily_briefing — staleness guard, grouping, per-SDR DMs,
exploration slot allocation, summary to dept-lead, mark-briefed rerun-safe."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel import daily_briefing, routing
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset_tof_tables():
    routing._ensure_user_cache()
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tof_lead_candidates"))
        conn.execute(text("DELETE FROM tof_enrichment_runs"))
        conn.execute(text("DELETE FROM tof_routing_state"))
        conn.execute(text("DELETE FROM tof_sf_user_cache"))
    yield


# ---------------------------------------------------------- fake send capture


class _Captured:
    """Records send_fn calls and returns a scripted response."""

    def __init__(self, ts_seed: int = 1000):
        self.calls: list[dict[str, Any]] = []
        self._ts = ts_seed

    def __call__(self, channel: str, text_: str, blocks: list | None = None, *, thread_ts: str | None = None):
        self._ts += 1
        self.calls.append(
            {
                "channel": channel,
                "text": text_,
                "blocks": blocks,
                "thread_ts": thread_ts,
            }
        )
        return {"ok": True, "ts": f"{self._ts}.0001"}


# ------------------------------------------------------- seed helpers


def _territory() -> dict[str, Any]:
    return {
        "default_owner_id": "005FALLBACK",
        "summary_recipients": ["hutch@x.com"],
        "segments": {
            "ENT": {
                "min_locations": 50,
                "rotation": [
                    {"email": "taylor@x.com", "slack_id": "U_TAY"},
                    {"email": "clay@x.com", "slack_id": "U_CLAY"},
                ],
            },
            "MM": {
                "min_locations": 10,
                "max_locations": 49,
                "rotation": [
                    {"email": "carlton@x.com", "slack_id": "U_CARL"},
                ],
            },
            "SMB": {
                "max_locations": 9,
                "rotation": [
                    {"email": "hutch@x.com", "slack_id": "U_HUTCH"},
                ],
            },
        },
    }


def _seed_user_cache(pairs: list[tuple[str, str]]):
    now = datetime.now(timezone.utc)
    engine = get_state_engine()
    with engine.begin() as conn:
        for user_id, email in pairs:
            conn.execute(
                text(
                    """INSERT INTO tof_sf_user_cache (email, user_id, name, is_active, cached_at)
                       VALUES (:e, :id, :n, 1, :t)
                       ON CONFLICT(email) DO UPDATE SET user_id=excluded.user_id"""
                ),
                {"e": email.lower(), "id": user_id, "n": email.split("@")[0], "t": now},
            )


def _seed_run(run_id: str, *, completed_at: datetime | None, status: str = "success",
              scanned: int = 10, suppressed: int = 0, written: int = 0):
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_enrichment_runs
                     (run_id, started_at, completed_at, status, scanned, suppressed, written_count)
                   VALUES (:r, :s, :c, :st, :sc, :su, :w)"""
            ),
            {
                "r": run_id,
                "s": (completed_at or datetime.now(timezone.utc)) - timedelta(hours=1),
                "c": completed_at,
                "st": status,
                "sc": scanned,
                "su": suppressed,
                "w": written,
            },
        )


def _seed_candidate(
    run_id: str,
    *,
    sdr_id: str | None,
    tier: str = "A",
    score: int = 85,
    domain: str = "acme.com",
    company: str = "Acme",
    status: str = "ready",
    location_count: int | None = 47,
):
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO tof_lead_candidates
                   (run_id, domain, company_name, email, first_name, last_name, title,
                    phone, icp_score, icp_tier, location_count, brand, ownership_type,
                    status, assigned_sdr_id)
                   VALUES (:r, :d, :c, :e, :fn, :ln, :t, NULL, :s, :tier, :lc,
                           'Arbys', 'franchise_group', :st, :sdr)"""
            ),
            {
                "r": run_id,
                "d": domain,
                "c": company,
                "e": f"person@{domain}",
                "fn": "Jane",
                "ln": "Doe",
                "t": "VP Ops",
                "s": score,
                "tier": tier,
                "lc": location_count,
                "st": status,
                "sdr": sdr_id,
            },
        )


# ============================================================ staleness guard


@pytest.mark.asyncio
async def test_staleness_guard_skips_when_run_old():
    _seed_run("old-run", completed_at=datetime.now(timezone.utc) - timedelta(hours=6))
    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert result["status"] == "skipped"
    assert "stale" in result["reason"]
    # Only one DM — to O.
    assert len(cap.calls) == 1
    assert cap.calls[0]["channel"] == daily_briefing._O_DM_CHANNEL
    assert "skipped" in cap.calls[0]["text"].lower()


@pytest.mark.asyncio
async def test_staleness_guard_when_no_run_exists():
    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "no_pipeline_run_found"


@pytest.mark.asyncio
async def test_staleness_guard_when_running():
    _seed_run("r1", completed_at=None, status="running")
    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert result["status"] == "skipped"
    assert "running" in result["reason"]


# =========================================================== per-SDR DMs


@pytest.mark.asyncio
async def test_primary_dms_sent_to_each_sdr():
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    _seed_user_cache([("005TAY", "taylor@x.com"), ("005CLA", "clay@x.com")])
    for i in range(3):
        _seed_candidate("r1", sdr_id="005TAY", tier="A", score=90 - i, domain=f"ent{i}.com")
    for i in range(2):
        _seed_candidate("r1", sdr_id="005CLA", tier="B", score=72 - i, domain=f"ent2-{i}.com")

    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert result["status"] == "success"
    assert result["sent"] == 2  # Taylor + Clay got DMs
    channels = {c["channel"] for c in cap.calls}
    assert "U_TAY" in channels
    assert "U_CLAY" in channels


@pytest.mark.asyncio
async def test_unresolved_slack_id_recorded_but_briefed():
    """SDR whose slack_id is PLACEHOLDER gets no DM, but candidates still
    marked briefed so tomorrow doesn't re-send."""
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    _seed_user_cache([("005MAT", "matt@x.com")])
    _seed_candidate("r1", sdr_id="005MAT", tier="A", score=88)

    cfg = _territory()
    cfg["segments"]["ENT"]["rotation"].append({"email": "matt@x.com", "slack_id": "PLACEHOLDER"})

    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(send_fn=cap, territory_cfg=cfg)
    assert "matt@x.com" in result["unresolved"]
    # DB was updated anyway (don't re-brief same run tomorrow).
    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(text("SELECT status FROM tof_lead_candidates")).fetchone()
    assert row[0] == "briefed"


# ============================================================= exploration


@pytest.mark.asyncio
async def test_exploration_slot_distributed_across_sdrs():
    """Tier C/D with ICP>=40 become exploration leads, round-robined to SDRs
    who have primary leads."""
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=15))
    _seed_user_cache([("005TAY", "taylor@x.com"), ("005CLA", "clay@x.com")])
    _seed_candidate("r1", sdr_id="005TAY", tier="A", score=85)
    _seed_candidate("r1", sdr_id="005CLA", tier="B", score=72)
    # 4 exploration leads — each SDR should get 2 (round-robin).
    for i in range(4):
        _seed_candidate(
            "r1",
            sdr_id=None,
            tier="C",
            score=55 - i,
            domain=f"exp{i}.com",
            company=f"Exp{i}",
        )

    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert result["status"] == "success"
    tay = result["per_sdr"]["taylor@x.com"]
    clay = result["per_sdr"]["clay@x.com"]
    assert tay["exploration"] + clay["exploration"] == 4


@pytest.mark.asyncio
async def test_exploration_below_threshold_excluded():
    """Tier D with ICP<40 should NOT appear in the exploration pool."""
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=15))
    _seed_user_cache([("005TAY", "taylor@x.com")])
    _seed_candidate("r1", sdr_id="005TAY", tier="A", score=85)
    _seed_candidate("r1", sdr_id=None, tier="D", score=20, domain="cold.com")

    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert result["per_sdr"]["taylor@x.com"]["exploration"] == 0


# =============================================================== summary


@pytest.mark.asyncio
async def test_summary_sent_to_recipients():
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    _seed_user_cache([("005TAY", "taylor@x.com")])
    _seed_candidate("r1", sdr_id="005TAY", tier="A", score=85)

    cap = _Captured()
    result = await daily_briefing.send_daily_briefing(
        send_fn=cap, territory_cfg=_territory()
    )
    assert "hutch@x.com" in result["summary_sent"]
    # One of the calls went to U_HUTCH with the summary.
    hutch_msgs = [c for c in cap.calls if c["channel"] == "U_HUTCH"]
    assert len(hutch_msgs) >= 1


# =============================================================== mark briefed


@pytest.mark.asyncio
async def test_mark_briefed_rerun_safe():
    """After a successful briefing, the same run's candidates are status='briefed'
    and a second invocation sends zero DMs."""
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=15))
    _seed_user_cache([("005TAY", "taylor@x.com")])
    _seed_candidate("r1", sdr_id="005TAY", tier="A", score=85)

    cap1 = _Captured()
    r1 = await daily_briefing.send_daily_briefing(send_fn=cap1, territory_cfg=_territory())
    assert r1["sent"] == 1

    cap2 = _Captured()
    r2 = await daily_briefing.send_daily_briefing(send_fn=cap2, territory_cfg=_territory())
    # Second run: no 'ready' candidates left → per_sdr empty → no DMs.
    assert r2["sent"] == 0


# ============================================================= thread replies


@pytest.mark.asyncio
async def test_thread_replies_when_more_than_top_n():
    """With >20 primary leads, overflow is posted as thread replies."""
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=15))
    _seed_user_cache([("005TAY", "taylor@x.com")])
    # 25 leads → 20 in body + 5 in thread (one thread message).
    for i in range(25):
        _seed_candidate("r1", sdr_id="005TAY", tier="A", score=100 - i, domain=f"ent{i}.com")

    cap = _Captured()
    await daily_briefing.send_daily_briefing(send_fn=cap, territory_cfg=_territory())

    thread_calls = [c for c in cap.calls if c.get("thread_ts") is not None]
    assert len(thread_calls) >= 1
    assert all(c["channel"] == "U_TAY" for c in thread_calls)


# ============================================================= dry-run


@pytest.mark.asyncio
async def test_dry_run_routes_to_one_channel():
    _seed_run("r1", completed_at=datetime.now(timezone.utc) - timedelta(minutes=15))
    _seed_user_cache([("005TAY", "taylor@x.com"), ("005CLA", "clay@x.com")])
    _seed_candidate("r1", sdr_id="005TAY", tier="A", score=90)
    _seed_candidate("r1", sdr_id="005CLA", tier="B", score=72)

    cap = _Captured()
    # Dry-run uses default territory loader; patch by providing territory via module.
    result = await daily_briefing.send_dry_run("U_TEST_PREVIEW", send_fn=cap)
    # Every DM routed to the preview channel.
    assert all(c["channel"] == "U_TEST_PREVIEW" for c in cap.calls)
    assert result["previews"] == 2

    # Candidates NOT marked briefed — dry-run is safe.
    engine = get_state_engine()
    with engine.begin() as conn:
        statuses = conn.execute(text("SELECT status FROM tof_lead_candidates")).fetchall()
    assert all(s[0] == "ready" for s in statuses)


@pytest.mark.asyncio
async def test_dry_run_no_run_sends_empty_notice():
    cap = _Captured()
    result = await daily_briefing.send_dry_run("U_O", send_fn=cap)
    assert result["status"] == "no_run"
    assert cap.calls  # a "nothing to preview" msg landed somewhere
    assert cap.calls[0]["channel"] == "U_O"
