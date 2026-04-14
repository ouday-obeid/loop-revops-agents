"""slt_metrics test fixtures + isolated DB bootstrap.

Follows the per-agent pattern from `agents/onboarding/tests/conftest.py` and
`agents/revops_support/tests/conftest.py`. Required because those agents also
register session-scoped autouse `_isolate_db` fixtures — when the full repo
suite runs, whichever fires last wins the env vars. Without our own fixture,
slt_metrics tests end up pointed at another agent's DB and miss the
`pipeline_snapshots` table.

The migration test (`test_migration_0004.py`) still calls `m.upgrade()`
explicitly per test; this conftest only provides the fresh DB + base schema.
"""
from __future__ import annotations

import importlib
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

    # Apply migrations 0004 + 0005 immediately — downstream tests (snapshotter,
    # jobs, movers, quota) read these tables regardless of collection order.
    m4 = importlib.import_module("shared.db.migrations.versions.0004_slt_revenue_metrics")
    m4.upgrade()
    m5 = importlib.import_module("shared.db.migrations.versions.0005_slt_rep_config")
    m5.upgrade()
    yield
