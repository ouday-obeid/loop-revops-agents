"""sales_reps agent test fixtures + isolated DB bootstrap.

Sibling `tests/conftest.py` at the repo root is only auto-loaded for tests
under `<root>/tests/…`. Since our tests live under `<root>/agents/sales_reps/
tests/…`, we bootstrap our own tempfile sqlite DB here (same pattern as
`agents/onboarding/tests/conftest.py` and `agents/revops_support/tests/
conftest.py`).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _sales_reps_isolate_db():
    tmp = tempfile.mkdtemp(prefix="sales_reps_test_")
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
