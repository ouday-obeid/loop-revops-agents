# Phase 0 amendment — additive PR required for Top of Funnel (Agent 1)

> **Status (2026-04-14):** LANDED. Every diff described below was absorbed into `main` via the Phase 1 specialist merges (e.g. `dd0fa56 merge(top_of_funnel)`, `aefa6a6 chore(test): enroll all 6 Phase 1 agent test dirs`). This document is retained as historical context for the amendment decision — no PR is open against it. See `_PHASE_0_AMENDMENT_REVIEW.md` for the landed-state review and regression coverage references below.

**Scope**: additive only. Zero edits to Phase 0 public surface area (`agent_base.py`, `salesforce_mcp.py`, existing `APPROVAL_TIERS` entries, existing `RATE_LIMITS` buckets). Everything here is a new row or a new file.

**Why a separate PR**: Phase 0 is sign-off frozen as of 2026-04-13. These additions are required for cron registration, the suppression-override tier, and per-agent SQLite isolation. They are scoped narrowly so the Phase 0 team can review in <30 minutes.

**Merge order**: this PR must land before D5 of the Top of Funnel build (pipeline orchestration ships D5 and consumes the new cron rows + helper).

---

## Diff 1 — `shared/runtime/schedule.py`

Append two `Job` rows to `SCHEDULE` list (after the `onboarding-*` block).

```python
    # Phase 1 — Agent 1 (Top of Funnel). See agents/top_of_funnel/RUNBOOK.md.
    Job(
        name="top-of-funnel-enrichment-pipeline",
        cron="0 2 * * 1-5",
        callable_path="agents.top_of_funnel.enrichment.pipeline:run_pipeline",
        description="Nightly Apollo+Clay enrichment + ICP scoring + SF Lead create",
    ),
    Job(
        name="top-of-funnel-daily-briefing",
        cron="55 7 * * 1-5",
        callable_path="agents.top_of_funnel.daily_briefing:send_daily_briefing",
        description="07:55 Mon–Fri SDR lead-list DMs + Hutch summary",
    ),
```

`infra/install_launchd.sh` generates plists from this list, so the two new jobs are picked up automatically on next bootstrap.

---

## Diff 2 — `shared/governance.py`

Append one entry to `APPROVAL_TIERS`.

```python
    # Phase 1 — Agent 1 (Top of Funnel). Used when an SDR / O overrides a
    # suppression hit (e.g., re-engaging a former customer). Requires written
    # justification to preserve the audit trail.
    "suppression_override": Tier(
        gate="slack_button", approver="o_or_dept_head", requires_justification=True
    ),
```

No changes to `RATE_LIMITS` — existing `nooks_sequences_daily=50` and `sf_lead_creation_daily=200` buckets cover this agent.

---

## Diff 3 — new file `shared/config/__init__.py`

```python
"""Shared YAML config loader. Phase 1 specialists call shared.config.loader:load_yaml."""
```

---

## Diff 4 — new file `shared/config/loader.py`

```python
"""load_yaml(name) — resolve YAML config by name from shared/config/ or an
agent's config/ dir. Caches results per (path, mtime) so hot-reload is safe.

Used by:
  - agents/top_of_funnel/suppression.py (loads shared/config/suppression_extras.yaml)
  - future specialists that ship shared seed data

Design note: YAML files use safe_load only. Never eval / never templated.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_SHARED_CONFIG_DIR = Path(__file__).parent


@lru_cache(maxsize=32)
def _load_cached(path_str: str, mtime_ns: int) -> dict[str, Any]:
    with open(path_str, "rb") as f:
        return yaml.safe_load(f) or {}


def load_yaml(name: str, *, agent_dir: Path | None = None) -> dict[str, Any]:
    """Resolve YAML by name. Checks agent_dir/config/ first, then shared/config/.

    Raises FileNotFoundError with both paths tried.
    """
    candidates: list[Path] = []
    if agent_dir is not None:
        candidates.append(agent_dir / "config" / f"{name}.yaml")
    candidates.append(_SHARED_CONFIG_DIR / f"{name}.yaml")

    for path in candidates:
        if path.exists():
            return _load_cached(str(path), path.stat().st_mtime_ns)

    tried = "\n  ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"load_yaml({name!r}) — tried:\n  {tried}")
```

---

## Diff 5 — new file `shared/config/suppression_extras.yaml`

Seed file with known competitors; extended over time by RevOps. Read by `agents/top_of_funnel/suppression.py`.

```yaml
# Domains auto-suppressed for outbound. RevOps adds new rows via PR.
# Competitors, customers-by-parent, and NDA-flagged domains.
competitors:
  - domain: olo.com
    reason: direct competitor
  - domain: flipdish.com
    reason: direct competitor
  - domain: toasttab.com
    reason: POS-adjacent competitor
  - domain: otter.ai  # NB: food delivery POS, not the transcription tool
    reason: direct competitor

nda_flagged: []

current_customer_parent_domains: []  # populated from SF Account.Type=Customer at runtime
```

---

## Diff 6 — `shared/db/connection.py` — add one helper

Append below `reset_cache`:

```python
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
```

If Phase 0 team prefers zero edits to `connection.py`, Top of Funnel will inline this helper locally at `agents/top_of_funnel/_state.py` — functionally identical, just not reusable across agents.

---

## Diff 7 — *(removed — landed upstream in `aefa6a6 chore(test): enroll all 6 Phase 1 agent test dirs in pytest testpaths`)*

---

## Diff 8 — `.env.example`

Append under `# --- Integrations ---` (or add a new `# --- Top of Funnel ---` block):

```
# --- Top of Funnel (Agent 1) ---
CLAY_MONTHLY_BUDGET_CREDITS=50000
NOOKS_CADENCE_SF_OBJECT=CampaignMember   # confirm at D1 kickoff w/ O
```

(APOLLO_API_KEY + CLAY_API_KEY already present in Phase 0 template — no change.)

---

## Test plan for this PR

```bash
cd $REVOPS_REPO_ROOT && source .venv/bin/activate

# 1. Existing Phase 0 suite stays green (no regressions):
pytest tests/ -v

# 2. New helpers are importable and work:
python -c "from shared.config.loader import load_yaml; print(load_yaml('suppression_extras'))"
python -c "from shared.db.connection import get_agent_engine; e = get_agent_engine('top_of_funnel'); print(e.url)"

# 3. Schedule parses:
python -c "from shared.runtime.schedule import SCHEDULE; print([j.name for j in SCHEDULE if j.name.startswith('top-of-funnel')])"

# 4. Governance tier resolves:
python -c "from shared.governance import APPROVAL_TIERS; print(APPROVAL_TIERS['suppression_override'])"
```

## Regression coverage (as landed)

Per-diff test references in the shipped tree. These are the concrete assertions backing each diff; keep in sync if the amendment is ever amended again.

| Diff | Test(s) | Location |
|------|---------|----------|
| 3–4 `shared.config.loader` | `test_competitor_domain_loader_uses_shared_first`, `test_competitor_domain_loader_falls_back_to_local` | `agents/top_of_funnel/tests/test_suppression.py:302, 317` |
| 5 `suppression_extras.yaml` (shared seed) | `test_8_competitor_domain_suppresses`, `test_suppression_extras_yaml_has_known_competitors` | `agents/top_of_funnel/tests/test_suppression.py:187`, `tests/test_db_connection.py` |
| 6 `get_agent_engine` | `test_get_agent_engine_enables_foreign_keys`, `test_get_agent_engine_isolates_agents` | `tests/test_db_connection.py` |

## Review checklist
- [x] 2 new Job rows in `SCHEDULE` — names unique, cron expressions valid
- [x] 1 new `APPROVAL_TIERS` entry — `suppression_override` tier+approver match existing patterns
- [x] `shared/config/` directory created with `__init__.py`, `loader.py`, `suppression_extras.yaml`
- [x] `shared/db/connection.py:get_agent_engine` added below `reset_cache`; cached with `lru_cache(16)`
- [x] `pyproject.toml:testpaths` gains `agents/top_of_funnel/tests` *(landed in `aefa6a6`)*
- [x] `.env.example` additions grouped under a `# --- Top of Funnel ---` block
- [x] No edits to `agent_base.py`, `salesforce_mcp.py`, `slack_dispatcher.py`, or existing tiers/buckets
- [x] Phase 0 test suite remains green
