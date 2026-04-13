"""RevOps Support agent test fixtures + isolated DB bootstrap."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _revops_support_isolate_db():
    tmp = tempfile.mkdtemp(prefix="revops_support_test_")
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
