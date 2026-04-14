"""Amendment DoD smokes — Diff 5 (suppression_extras seed) + Diff 6 (get_agent_engine).

These assert shipped behavior of the two artifacts the Phase 0 amendment
introduced and complement the TOF-local suppression loader tests in
`agents/top_of_funnel/tests/test_suppression.py` (which use tmp fixtures).
"""
from __future__ import annotations

from pathlib import Path

import yaml
from sqlalchemy import text


def test_suppression_extras_yaml_has_known_competitors():
    """Diff 5: the shipped `shared/config/suppression_extras.yaml` includes the
    canonical POS/ordering competitors referenced by test_suppression.py."""
    repo_root = Path(__file__).resolve().parents[1]
    path = repo_root / "shared" / "config" / "suppression_extras.yaml"
    data = yaml.safe_load(path.read_text()) or {}
    domains = {row["domain"] for row in data.get("competitors", [])}
    assert {"olo.com", "flipdish.com", "toasttab.com", "otter.ai"} <= domains, domains


def test_get_agent_engine_enables_foreign_keys(tmp_path, monkeypatch):
    """Diff 6: `shared.db.connection.get_agent_engine` returns an engine with
    SQLite foreign-key enforcement enabled via PRAGMA."""
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from shared.db.connection import get_agent_engine

    get_agent_engine.cache_clear()
    engine = get_agent_engine("top_of_funnel")
    with engine.connect() as conn:
        enabled = conn.execute(text("PRAGMA foreign_keys")).scalar()
    assert enabled == 1


def test_get_agent_engine_isolates_agents(tmp_path, monkeypatch):
    """Diff 6: each agent gets its own state.db under agents/<name>/state.db."""
    monkeypatch.setenv("REVOPS_REPO_ROOT", str(tmp_path))
    from shared.db.connection import get_agent_engine

    get_agent_engine.cache_clear()
    e1 = get_agent_engine("top_of_funnel")
    e2 = get_agent_engine("sales_reps")
    assert str(e1.url) != str(e2.url)
    assert "top_of_funnel/state.db" in str(e1.url)
    assert "sales_reps/state.db" in str(e2.url)
