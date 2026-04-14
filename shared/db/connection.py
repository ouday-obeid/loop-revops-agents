"""DB connection — keyed off REVOPS_DB_URL.

SQLite default for local dev; Postgres for Phase 4 GCP migration.
All agent code routes through this module — never construct engines elsewhere.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from shared.secrets import get_config


def _default_sqlite_url() -> str:
    root = Path(get_config("REVOPS_REPO_ROOT") or Path(__file__).resolve().parents[2])
    db_path = root / "shared" / "db" / "loop_revops.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def _resolve_db_url() -> str:
    url = get_config("REVOPS_DB_URL")
    if url:
        # Allow ${REVOPS_REPO_ROOT} expansion in .env values
        return os.path.expandvars(url)
    return _default_sqlite_url()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    url = _resolve_db_url()
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        with engine.connect() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
    return engine


@lru_cache(maxsize=1)
def _session_factory() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def get_session() -> Session:
    return _session_factory()()


def init_schema() -> None:
    """Apply schema.sql to the current engine. Idempotent."""
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text()
    engine = get_engine()
    dialect = engine.dialect.name
    with engine.begin() as conn:
        for statement in _iter_statements(sql, dialect):
            conn.execute(text(statement))


def _iter_statements(sql: str, dialect: str):
    """Split schema.sql on ;. Skip dialect-gated blocks (-- @dialect postgres)."""
    buf: list[str] = []
    skip = False
    for raw in sql.splitlines():
        line = raw.strip()
        if line.startswith("-- @dialect"):
            target = line.split()[-1]
            skip = target != dialect
            continue
        if line.startswith("-- @end"):
            skip = False
            continue
        if skip or line.startswith("--") or not line:
            continue
        buf.append(raw)
        if line.endswith(";"):
            yield "\n".join(buf).rstrip(";").strip()
            buf = []


def reset_cache() -> None:
    """For tests: drop cached engine/session factory."""
    get_engine.cache_clear()
    _session_factory.cache_clear()


@lru_cache(maxsize=16)
def get_agent_engine(agent_name: str) -> Engine:
    """Per-agent SQLite engine at agents/<agent>/state.db.

    Used by Phase 1 specialists that keep agent-local state (e.g. Top of
    Funnel's Clay credit ledger, suppression cache, routing round-robin
    cursor). Shared state (approval_gates, rate_limits, audit_log) continues
    to route through get_engine().
    """
    root = Path(get_config("REVOPS_REPO_ROOT") or Path(__file__).resolve().parents[2])
    db_path = root / "agents" / agent_name / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
    return engine
