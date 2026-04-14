"""D9 end-to-end integration test — fully mocked Apollo/Clay/SF.

Exercises the full pipeline → routing → daily_briefing flow:
  1. Pipeline ingests 5 Apollo accounts, enriches, routes, buffers, writes.
  2. Daily briefing groups by SDR, DMs each rotation slot, summary to Hutch.
  3. Dept-head access: Hutch's email resolves as dept_head via routing helper.

Doesn't touch a real SF sandbox — that integration test lives in
tests/integration/ and is env-gated via SF_SANDBOX_ORG_ALIAS (runs D10+ once O
shares access).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel import daily_briefing, routing
from agents.top_of_funnel.enrichment import pipeline
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset_all():
    routing._ensure_user_cache()
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tof_lead_candidates"))
        conn.execute(text("DELETE FROM tof_enrichment_runs"))
        conn.execute(text("DELETE FROM tof_routing_state"))
        conn.execute(text("DELETE FROM tof_sf_user_cache"))
        conn.execute(text("DELETE FROM apollo_query_cache"))
        conn.execute(text("DELETE FROM clay_credit_ledger"))
        conn.execute(text("DELETE FROM suppression_cache"))
    yield


# ------------------------------------------------- fakes


class _ScriptedHTTP:
    def __init__(self, by_endpoint: dict[str, list[Any]]):
        self._by_endpoint = {k: list(v) for k, v in by_endpoint.items()}

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]):
        for key, queue in self._by_endpoint.items():
            if key in url and queue:
                return _Resp(200, queue.pop(0))
        return _Resp(200, {})

    async def aclose(self):
        pass


class _Resp:
    def __init__(self, status: int, body: dict[str, Any]):
        self.status_code = status
        self._b = body
        self.text = ""

    def json(self):
        return self._b


def _accounts(*domains: str, locs: int = 47) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "primary_domain": d,
                "name": d.split(".")[0].title(),
                "num_locations": locs,
                "estimated_num_employees": 450,
                "industry": "Restaurants",
            }
            for d in domains
        ]
    }


def _people(email: str, first: str, last: str, title: str = "VP Operations") -> dict[str, Any]:
    return {
        "people": [
            {"email": email, "first_name": first, "last_name": last, "title": title}
        ]
    }


def _seed_user_cache(pairs: list[tuple[str, str]]):
    now = datetime.now(timezone.utc)
    engine = get_state_engine()
    with engine.begin() as conn:
        for uid, email in pairs:
            conn.execute(
                text(
                    """INSERT INTO tof_sf_user_cache (email, user_id, name, is_active, cached_at)
                       VALUES (:e, :u, :n, 1, :t)
                       ON CONFLICT(email) DO UPDATE SET user_id=excluded.user_id,
                                                        is_active=1,
                                                        cached_at=excluded.cached_at"""
                ),
                {"e": email.lower(), "u": uid, "n": email.split("@")[0], "t": now},
            )


def _territory() -> dict[str, Any]:
    return {
        "default_owner_id": "005FALLBACK",
        "summary_recipients": ["hutch@tryloop.ai"],
        "dept_heads": ["hutch@tryloop.ai", "charles@tryloop.ai"],
        "segments": {
            "ENT": {
                "min_locations": 50,
                "rotation": [
                    {"email": "taylor@tryloop.ai", "slack_id": "U_TAY"},
                    {"email": "clay@tryloop.ai", "slack_id": "U_CLAY"},
                ],
            },
            "MM": {
                "min_locations": 10,
                "max_locations": 49,
                "rotation": [
                    {"email": "carlton@tryloop.ai", "slack_id": "U_CARL"},
                ],
            },
            "SMB": {
                "max_locations": 9,
                "rotation": [
                    {"email": "hutch@tryloop.ai", "slack_id": "U_HUTCH"},
                ],
            },
        },
    }


# =========================================================== E2E pipeline


@pytest.mark.asyncio
async def test_pipeline_routes_and_briefs(monkeypatch):
    """5 accounts in → routed to MM bucket (all 47 locs) → Carlton gets 5 A/B
    DMs. Run then briefed; no candidates 'ready' after briefing."""
    monkeypatch.setattr(routing, "load_territory", _territory)

    # Seed SF user cache so routing resolves rotation emails → user_id.
    _seed_user_cache([("005CAR", "carlton@tryloop.ai")])

    http = _ScriptedHTTP({
        "/accounts/search": [_accounts("a.com", "b.com", "c.com", "d.com", "e.com")],
        "/mixed_people/search": [
            _people(f"p@{d}.com", "P", str(i))
            for i, d in enumerate(["a", "b", "c", "d", "e"])
        ],
    })

    gate_calls: list[dict[str, Any]] = []

    def fake_gate(**kw):
        gate_calls.append(kw)
        return 501

    creates: list[dict[str, Any]] = []

    def fake_create(sobject: str, fields: dict[str, Any], **kw: Any):
        creates.append(fields)
        return {"id": f"00Q{len(creates):03d}"}

    # Fully mocked SF query — nothing duplicate.
    def fake_sf_query(q: str):
        return {"records": []}

    # Describe — all custom fields present so payload uses real fields.
    def fake_describe(name: str):
        return {
            "fields": [
                {"name": n}
                for n in (
                    "ICP_Score__c", "ICP_Tier__c", "Brand__c",
                    "Ownership_Type__c", "Location_Count__c",
                )
            ]
        }

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=False,
        sf_query=fake_sf_query,
        create_fn=fake_create,
        describe_fn=fake_describe,
        create_gate_fn=fake_gate,
    )

    assert result["status"] in ("success", "partial")
    assert result["scanned"] == 5
    assert result["buffered"] == 5
    assert result["written"] == 5
    assert len(creates) == 5
    # One gate for the whole batch of 5.
    assert len(gate_calls) == 1
    assert gate_calls[0]["action_type"] == "bulk_update_small"

    # Every written lead carries OwnerId=Carlton (MM is single-member rotation).
    owners = {f.get("OwnerId") for f in creates}
    assert owners == {"005CAR"}

    # Now run daily briefing — need at least one A/B tier row. Force one via
    # DB patch: set candidates to tier A so they go into primary bucket.
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE tof_lead_candidates SET icp_tier = 'A', status = 'ready'"
        ))
        # Also reset run so staleness-guard doesn't veto (it was just written).
        conn.execute(text(
            "UPDATE tof_enrichment_runs SET completed_at = :t"
        ), {"t": datetime.now(timezone.utc)})

    sent: list[dict[str, Any]] = []

    def fake_send(channel: str, text_: str, blocks: list | None = None, *, thread_ts: str | None = None):
        sent.append({"channel": channel, "text": text_, "blocks": blocks, "thread_ts": thread_ts})
        return {"ok": True, "ts": f"{len(sent)}.0"}

    bfr = await daily_briefing.send_daily_briefing(
        send_fn=fake_send, territory_cfg=_territory()
    )
    assert bfr["status"] == "success"
    # Carlton's DM + Hutch summary.
    assert "U_CARL" in {s["channel"] for s in sent}
    assert "U_HUTCH" in {s["channel"] for s in sent}
    assert bfr["per_sdr"]["carlton@tryloop.ai"]["primary"] == 5

    # Rerun-safe — second briefing finds no 'ready' candidates.
    sent2: list[dict[str, Any]] = []
    bfr2 = await daily_briefing.send_daily_briefing(
        send_fn=lambda *a, **k: (sent2.append(a) or {"ok": True, "ts": "x"}),
        territory_cfg=_territory(),
    )
    assert bfr2["sent"] == 0


# =========================================================== dept-head


def test_hutch_is_dept_head():
    assert routing.is_dept_head("hutch@tryloop.ai", _territory()) is True
    assert routing.is_dept_head("HUTCH@TRYLOOP.AI", _territory()) is True  # case-insensitive


def test_charles_is_dept_head():
    assert routing.is_dept_head("charles@tryloop.ai", _territory()) is True


def test_random_sdr_is_not_dept_head():
    assert routing.is_dept_head("taylor@tryloop.ai", _territory()) is False


def test_empty_email_is_not_dept_head():
    assert routing.is_dept_head("", _territory()) is False
    assert routing.is_dept_head(None, _territory()) is False


def test_default_territory_dept_heads_includes_hutch_and_charles(monkeypatch):
    """Verify the real territory.yaml ships with Hutch + Charles as dept_heads."""
    cfg = routing.load_territory()
    heads = {e.lower() for e in (cfg.get("dept_heads") or [])}
    assert "hutch@tryloop.ai" in heads
    assert "charles@tryloop.ai" in heads
