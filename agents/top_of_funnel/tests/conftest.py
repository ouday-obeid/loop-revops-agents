"""Pytest fixtures for Top of Funnel agent.

Self-contained because pytest does not auto-discover the root tests/conftest.py
when collecting under a sibling path (agents/top_of_funnel/tests/). We mirror
Phase 0's root conftest isolation pattern + apply the agent's state.sql on top.

Side effects (session-scope, autouse):
  - Tempdir SQLite via REVOPS_DB_URL
  - REVOPS_REPO_ROOT pinned to tempdir (so get_agent_engine lands there too)
  - Phase 0 shared schema + agent state.sql applied
  - Offline defaults for Apollo/Clay/Slack so no test hits external APIs
  - SF_ORG_ALIAS forced to the sandbox alias
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import text


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _isolate_env():
    """Sandbox DB + repo root + offline defaults for every test in this package."""
    tmp = tempfile.mkdtemp(prefix="tof_test_")
    db_file = Path(tmp) / "test.db"
    os.environ["REVOPS_REPO_ROOT"] = tmp
    os.environ["REVOPS_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["REVOPS_KNOWLEDGE_BACKEND"] = "chromadb_local"
    os.environ["REVOPS_SECRETS_BACKEND"] = "dotenv"
    os.environ["SLACK_DEV_GUARD"] = "0"
    os.environ.setdefault("APOLLO_API_KEY", "test-apollo")
    os.environ.setdefault("CLAY_API_KEY", "test-clay")
    os.environ.setdefault("CLAY_MONTHLY_BUDGET_CREDITS", "10000")
    os.environ.setdefault("NOOKS_CADENCE_SF_OBJECT", "CampaignMember")
    os.environ["SF_ORG_ALIAS"] = "salesops-sandbox"

    from shared.db import connection as c
    c.reset_cache()
    from shared.db.connection import init_schema
    init_schema()

    from agents.top_of_funnel import state as agent_state
    agent_state.reset_cache()
    agent_sql = (Path(__file__).parent.parent / "state.sql").read_text()
    engine = agent_state.get_state_engine()
    with engine.begin() as conn:
        for stmt in _iter_statements(agent_sql):
            conn.execute(text(stmt))

    yield


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def closed_won_sample(fixtures_dir: Path) -> list[dict]:
    """~30 closed-won accounts from SF — populated by O on D1."""
    import json

    path = fixtures_dir / "closed_won_sample.json"
    if not path.exists():
        pytest.skip("closed_won_sample.json not populated — pending D1 prerequisite from O")
    return json.loads(path.read_text())


@pytest.fixture
def cold_leads_sample(fixtures_dir: Path) -> list[dict]:
    import json

    path = fixtures_dir / "cold_leads_sample.json"
    if not path.exists():
        pytest.skip("cold_leads_sample.json not populated")
    return json.loads(path.read_text())


def _iter_statements(sql: str):
    buf: list[str] = []
    for raw in sql.splitlines():
        line = raw.strip()
        if line.startswith("--") or not line:
            continue
        buf.append(raw)
        if line.endswith(";"):
            yield "\n".join(buf).rstrip(";").strip()
            buf = []
