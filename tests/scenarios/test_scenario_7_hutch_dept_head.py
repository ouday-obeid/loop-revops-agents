"""Scenario 7 — Hutch dept-head access smoke.

Monday item: 11736878004
Path: `routing.is_dept_head(email, territory.yaml)` returns True for Hutch and
Charles, False for SDRs. Exercises both the helper (with an injected cfg) and
the real checked-in territory.yaml so a rotation edit doesn't silently drop
the dept-head list.

Two sub-scenarios:

  1. Routing helper + checked-in config: pure-Python, always runs.
  2. Live Slack DM from Hutch → `@oo ...` command accepted without O gating:
     marked skip — requires a real Slack workspace + Hutch's user-id at
     runtime. Scaffolded so when the integration test harness is ready, the
     skip marker can be dropped without restructuring.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agents.top_of_funnel import routing


def test_hutch_is_dept_head(tof_territory):
    assert routing.is_dept_head("hutch@tryloop.ai", tof_territory) is True
    assert routing.is_dept_head("HUTCH@TRYLOOP.AI", tof_territory) is True


def test_charles_is_dept_head(tof_territory):
    assert routing.is_dept_head("charles@tryloop.ai", tof_territory) is True


def test_rotation_sdr_is_not_dept_head(tof_territory):
    assert routing.is_dept_head("carlton@tryloop.ai", tof_territory) is False


def test_blank_email_never_dept_head(tof_territory):
    assert routing.is_dept_head("", tof_territory) is False
    assert routing.is_dept_head(None, tof_territory) is False  # type: ignore[arg-type]


def test_real_territory_yaml_includes_hutch_and_charles():
    """Canary against the checked-in territory.yaml under version control.

    If someone trims the dept_heads list in a future rotation edit, this test
    catches it before Phase 1 ships. Mirrors
    agents/top_of_funnel/tests/test_end_to_end.py::test_default_territory_dept_heads_includes_hutch_and_charles.
    """
    cfg = routing.load_territory()
    heads = {e.lower() for e in (cfg.get("dept_heads") or [])}
    assert "hutch@tryloop.ai" in heads
    assert "charles@tryloop.ai" in heads
    # Also sanity-check the rotation files haven't been emptied out.
    assert cfg.get("segments", {}).get("ENT")
    assert cfg.get("segments", {}).get("MM")
    assert cfg.get("segments", {}).get("SMB")


@pytest.mark.skip(reason="requires live Hutch DM — scaffold only, wire once Slack harness is ready")
def test_hutch_dm_accepted_end_to_end():  # pragma: no cover
    """Live integration: Hutch DMs OO and the ToF daily-briefing command runs.

    Flow (pending infra):
      1. Slack bot receives an `app_mention` or DM from Hutch's real user-id.
      2. Dispatcher parses `@oo tof daily`, routes to top_of_funnel.handle.
      3. routing.is_dept_head gate lets it through without O-only gating.
      4. Daily briefing returns a `{"status": ...}` dict.

    TODO: implement once the Slack Socket Mode integration harness exists.
    Today this is a no-op placeholder so the scenario's DoD intent stays
    visible in the test inventory.
    """
    raise NotImplementedError  # pragma: no cover
