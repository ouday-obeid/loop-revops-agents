"""Per-agent SQLite state — local until Phase 0 amendment lands.

Once `shared.db.connection.get_agent_engine` merges (see
_PHASE_0_AMENDMENT_PR.md, Diff 6), this file becomes a one-line re-export so
call sites don't churn.

Tables are defined in state.sql and applied by tests/conftest.py (tests) and
by scripts/init_agent_state.sh (production bootstrap).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, text


@lru_cache(maxsize=1)
def get_state_engine() -> Engine:
    """SQLite engine at agents/top_of_funnel/state.db (or REVOPS_REPO_ROOT/agents/...)."""
    root_env = os.environ.get("REVOPS_REPO_ROOT")
    if root_env:
        agent_dir = Path(root_env) / "agents" / "top_of_funnel"
    else:
        agent_dir = Path(__file__).parent
    agent_dir.mkdir(parents=True, exist_ok=True)
    db_path = agent_dir / "state.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
    return engine


def reset_cache() -> None:
    """For tests: drop cached engine."""
    get_state_engine.cache_clear()
