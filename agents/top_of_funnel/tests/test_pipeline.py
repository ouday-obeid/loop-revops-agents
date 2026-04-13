"""D5 tests for pipeline.run_pipeline — dry-run, gate creation, Clay gating,
suppression propagation, dedup handling, candidate buffering."""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from agents.top_of_funnel.enrichment import apollo_client, clay_client, pipeline
from agents.top_of_funnel.state import get_state_engine


@pytest.fixture(autouse=True)
def _reset_agent_tables():
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tof_lead_candidates"))
        conn.execute(text("DELETE FROM tof_enrichment_runs"))
        conn.execute(text("DELETE FROM apollo_query_cache"))
        conn.execute(text("DELETE FROM clay_credit_ledger"))
        conn.execute(text("DELETE FROM suppression_cache"))
    yield


# ----------------------------------------------------------- fake HTTP / SF


class _ScriptedHTTP:
    """Returns responses in order, keyed by endpoint substring."""

    def __init__(self, by_endpoint: dict[str, list[Any]]):
        self._by_endpoint = {k: list(v) for k, v in by_endpoint.items()}
        self.calls: list[str] = []

    async def post(self, url: str, *, json: dict[str, Any], headers: dict[str, str]):
        self.calls.append(url)
        for key, queue in self._by_endpoint.items():
            if key in url and queue:
                nxt = queue.pop(0)
                if isinstance(nxt, Exception):
                    raise nxt
                return _Response(200, nxt)
        return _Response(200, {})

    async def aclose(self):
        pass


class _Response:
    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self) -> dict[str, Any]:
        return self._body


def _accounts_response(*domains: str) -> dict[str, Any]:
    return {
        "accounts": [
            {
                "primary_domain": d,
                "name": d.split(".")[0].title(),
                "num_locations": 47,
                "estimated_num_employees": 450,
                "industry": "Restaurants",
            }
            for d in domains
        ]
    }


def _people_response(email: str, first: str, last: str, title: str = "VP Operations") -> dict[str, Any]:
    return {
        "people": [
            {
                "email": email,
                "first_name": first,
                "last_name": last,
                "title": title,
            }
        ]
    }


def _fake_sf_clean(q: str) -> dict[str, Any]:
    return {"records": []}


def _fake_describe(existing: set[str]):
    def fn(name: str):
        return {"fields": [{"name": n} for n in existing]}

    return fn


# ----------------------------------------------------------- dry-run paths


@pytest.mark.asyncio
async def test_dry_run_buffers_but_does_not_write():
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("franchisee.com")],
        "/mixed_people/search": [_people_response("jane@franchisee.com", "Jane", "Doe")],
    })

    creates: list[Any] = []

    def capture_create(*a, **kw):
        creates.append((a, kw))
        return {"id": "shouldnothappen"}

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=True,
        sf_query=_fake_sf_clean,
        create_fn=capture_create,
        describe_fn=_fake_describe(set()),
    )

    assert result["status"] == "success"
    assert result["scanned"] == 1
    assert result["buffered"] == 1
    assert result["written"] == 0
    assert creates == []  # dry_run shorts-out before writes

    # candidate row persisted
    engine = get_state_engine()
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT domain, icp_tier FROM tof_lead_candidates")).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "franchisee.com"


@pytest.mark.asyncio
async def test_suppression_flags_candidate_and_skips_write():
    # Clay-enriched email is suppressed via pre-seeded cache.
    engine = get_state_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                """INSERT INTO suppression_cache (email, suppressed, reason, source, checked_at)
                   VALUES ('jane@supp.com', 1, 'manual', 'manual', CURRENT_TIMESTAMP)"""
            )
        )

    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("supp.com")],
        "/mixed_people/search": [_people_response("jane@supp.com", "Jane", "Doe")],
    })

    creates: list[Any] = []

    def capture_create(*a, **kw):
        creates.append(kw)
        return {"id": "new123"}

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=False,
        sf_query=_fake_sf_clean,
        create_fn=capture_create,
        describe_fn=_fake_describe(set()),
        auto_approve_gate=True,
    )

    assert result["buffered"] == 1
    assert result["suppressed"] == 1
    assert result["written"] == 0
    assert creates == []  # suppressed → status='suppressed' in buffer → not writable


# ----------------------------------------------------------- gate creation


@pytest.mark.asyncio
async def test_small_batch_creates_small_gate():
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("a.com", "b.com", "c.com")],
        "/mixed_people/search": [
            _people_response("a@a.com", "A", "Aa"),
            _people_response("b@b.com", "B", "Bb"),
            _people_response("c@c.com", "C", "Cc"),
        ],
    })

    gate_calls: list[dict] = []

    def fake_gate(**kw):
        gate_calls.append(kw)
        return 99

    def fake_create(sobject, fields, **kw):
        return {"id": f"00Q{fields['Email']}"}

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=False,
        sf_query=_fake_sf_clean,
        create_fn=fake_create,
        describe_fn=_fake_describe({"ICP_Score__c", "ICP_Tier__c", "Brand__c", "Ownership_Type__c", "Location_Count__c"}),
        create_gate_fn=fake_gate,
    )

    assert len(gate_calls) == 1
    assert gate_calls[0]["action_type"] == "bulk_update_small"
    assert gate_calls[0]["payload"]["count"] == 3
    assert result["approval_gate_id"] == 99
    assert result["written"] == 3


@pytest.mark.asyncio
async def test_single_candidate_no_gate_needed():
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("solo.com")],
        "/mixed_people/search": [_people_response("x@solo.com", "X", "Y")],
    })

    gate_calls: list[dict] = []

    def fake_gate(**kw):
        gate_calls.append(kw)
        return 1

    creates: list[Any] = []

    def fake_create(sobject, fields, **kw):
        creates.append(fields)
        return {"id": "00Q001"}

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=False,
        sf_query=_fake_sf_clean,
        create_fn=fake_create,
        describe_fn=_fake_describe(set()),
        create_gate_fn=fake_gate,
    )

    # count=1 → single_record_update tier is auto_notify → no gate row created.
    assert gate_calls == []
    assert result["approval_gate_id"] is None
    assert result["written"] == 1


# ----------------------------------------------------------- Clay budget


@pytest.mark.asyncio
async def test_clay_budget_exceeded_continues_without_crashing():
    """Ledger pre-seeded at cap. Pipeline should NOT crash — it should still
    buffer candidates (Apollo-only data). Clay skip_reason is recorded."""
    budget = clay_client.CreditBudget(1)
    budget.spend(1)  # now at cap

    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("x.com", "y.com")],
        "/mixed_people/search": [
            _people_response("x@x.com", "X", "Xx"),
            _people_response("y@y.com", "Y", "Yy"),
        ],
    })

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=True,
        sf_query=_fake_sf_clean,
        describe_fn=_fake_describe(set()),
        clay_budget=budget,
    )

    assert result["scanned"] == 2
    assert result["buffered"] == 2
    # Clay skipped — every account with budget_exceeded.
    assert result["clay_skipped"] == 2


@pytest.mark.asyncio
async def test_grade_floor_skips_clay_not_buffer():
    """Grade-D cold lead: Clay gets skipped, but candidate still buffers
    (the ICP score might still be C tier → exploration slot eligible)."""
    # No budget seed; use default.
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("cold.com")],
        "/mixed_people/search": [_people_response("x@cold.com", "X", "Xx")],
    })

    # Force grade below floor by injecting it in the account payload via monkeypatch.
    import agents.top_of_funnel.enrichment.pipeline as pl

    orig = pl._enrich_one_account

    async def lower_grade(account, **kw):
        account["apollo_grade"] = "D"
        return await orig(account, **kw)

    pl._enrich_one_account = lower_grade
    try:
        result = await pipeline.run_pipeline(
            search_filters={"x": 1},
            http_client=http,
            dry_run=True,
            grade_floor="B",
            sf_query=_fake_sf_clean,
            describe_fn=_fake_describe(set()),
        )
    finally:
        pl._enrich_one_account = orig

    assert result["buffered"] == 1
    assert result["clay_skipped"] == 1


# ----------------------------------------------------------- dedup inside pipeline


@pytest.mark.asyncio
async def test_dedup_skip_counted_and_not_written():
    """SF says a Contact with this email already exists — pipeline must not
    call create_record for that candidate."""
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("dupe.com")],
        "/mixed_people/search": [_people_response("dup@dupe.com", "D", "Up")],
    })

    def sf_q(q: str):
        if "FROM Contact" in q:
            return {"records": [{"Id": "003EXISTING"}]}
        return {"records": []}

    creates: list[Any] = []

    def fake_create(sobject, fields, **kw):
        creates.append(fields)
        return {"id": "00Qnew"}

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=False,
        sf_query=sf_q,
        create_fn=fake_create,
        describe_fn=_fake_describe(set()),
    )

    assert result["dedup_skipped"] == 1
    assert result["written"] == 0
    assert creates == []  # dedup blocked the create

    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status, error_message FROM tof_lead_candidates")
        ).fetchone()
    assert row[0] == "suppressed"
    assert "dedup" in (row[1] or "")


# --------------------------------------------------------------- error paths


@pytest.mark.asyncio
async def test_empty_apollo_returns_success_with_zero():
    http = _ScriptedHTTP({
        "/accounts/search": [{"accounts": []}],
    })

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=True,
        sf_query=_fake_sf_clean,
        describe_fn=_fake_describe(set()),
    )

    assert result["status"] == "success"
    assert result["scanned"] == 0
    assert result["buffered"] == 0


@pytest.mark.asyncio
async def test_enrich_single_dry_run():
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("acme.com")],
        "/mixed_people/search": [_people_response("x@acme.com", "A", "B")],
    })

    result = await pipeline.enrich_single("acme.com", write=False)
    # Apollo returned zero because we didn't inject http — but soft-fail
    # means this returns a valid envelope.
    assert "Enrich" in result["text"]
    assert "acme.com" in result["text"]
    assert result["report"]["dry_run"] is True


@pytest.mark.asyncio
async def test_enrich_single_rejects_bad_input():
    r = await pipeline.enrich_single("")
    assert "doesn't look like a domain" in r["text"]

    r = await pipeline.enrich_single("notadomain")
    assert "doesn't look like a domain" in r["text"]


# ----------------------------------------------------- run record persistence


@pytest.mark.asyncio
async def test_run_record_written_with_tallies():
    http = _ScriptedHTTP({
        "/accounts/search": [_accounts_response("a.com", "b.com")],
        "/mixed_people/search": [
            _people_response("a@a.com", "A", "A"),
            _people_response("b@b.com", "B", "B"),
        ],
    })

    result = await pipeline.run_pipeline(
        search_filters={"x": 1},
        http_client=http,
        dry_run=True,
        sf_query=_fake_sf_clean,
        describe_fn=_fake_describe(set()),
    )

    engine = get_state_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """SELECT run_id, status, scanned, suppressed
                   FROM tof_enrichment_runs ORDER BY id DESC LIMIT 1"""
            )
        ).fetchone()
    assert row[0] == result["run_id"]
    assert row[1] == "success"
    assert row[2] == 2
