"""CS agent test fixtures + isolated DB bootstrap (agent-local)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _cs_isolate_db():
    """Isolated SQLite per test session — mirrors root tests/conftest.py.

    pytest does not auto-load sibling conftest files, so agent-local tests
    need their own DB bootstrap.
    """
    tmp = tempfile.mkdtemp(prefix="revops_cs_test_")
    db_file = Path(tmp) / "test.db"
    os.environ["REVOPS_REPO_ROOT"] = tmp
    os.environ["REVOPS_DB_URL"] = f"sqlite:///{db_file}"
    os.environ["REVOPS_KNOWLEDGE_BACKEND"] = "chromadb_local"
    os.environ["REVOPS_SECRETS_BACKEND"] = "dotenv"
    os.environ["SLACK_DEV_GUARD"] = "0"

    from shared.db import connection as c
    c.reset_cache()
    from shared.db.connection import init_schema
    init_schema()
    yield


@pytest.fixture
def cs_payload():
    """Factory for a minimal slack payload to the CS dispatcher."""
    def _make(text: str = "", user: str = "U_TEST", channel: str = "C_TEST") -> dict:
        return {"text": text, "user": user, "channel": channel}
    return _make
