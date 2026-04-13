"""slt_metrics agent test fixtures + isolated DB bootstrap.

Sibling `tests/conftest.py` at the repo root is only auto-loaded for tests
under `<root>/tests/…` or when pytest is invoked bare (collecting every
testpath listed in pyproject.toml). Any targeted run — `pytest agents/
slt_metrics/tests/` or an IDE click-to-run — walks up from the target
directory, and `tests/conftest.py` is a sibling, not an ancestor, so its
session-autouse fixture never fires. Result: `no such table: agent_runs`
the moment a test touches the DB.

Mirror the same per-agent bootstrap already in place for onboarding,
revops_support, and sales_reps so this agent's tests are self-sufficient
regardless of how pytest is invoked.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _slt_metrics_isolate_db():
    tmp = tempfile.mkdtemp(prefix="slt_metrics_test_")
    db_file = Path(tmp) / "test.db"
    os.environ["REVOPS_REPO_ROOT"] = tmp
    os.environ["REVOPS_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["REVOPS_KNOWLEDGE_BACKEND"] = "chromadb_local"
    os.environ["REVOPS_SECRETS_BACKEND"] = "dotenv"
    os.environ["SLACK_DEV_GUARD"] = "0"
    os.environ.setdefault("SF_ORG_ALIAS", "salesops")

    from shared.db import connection as c
    c.reset_cache()
    from shared.db.connection import init_schema
    init_schema()
    yield
