# loop-revops-agents

Loop AI 7-agent RevOps system. Phase 0 ships the shared foundation + OO orchestrator; Phases 1–3 add six specialist agents; Phase 4 lifts to GCP.

## Quick start (MacBook Pro, primary host)

```bash
cd /Users/ottimate/loop-revops-agents
bash infra/bootstrap.sh
source .venv/bin/activate
bash scripts/run_migrations.sh
bash scripts/load_seed_data.sh          # ingests /Users/ottimate/sf-admin/knowledge/*.md
pytest --cov=shared --cov=agents/oo     # expect green, ≥80% coverage
bash infra/install_launchd.sh
```

In Slack DM to the OO bot: `@oo ping` → `pong`.

## Layout
- `shared/` — DB, governance, MCPs, AgentBase, Slack dispatcher, runtime scheduler, lint plugin
- `agents/oo/` — OO orchestrator (dispatcher, monitor, health, briefings, classifier, main, skill, runbook)
- `infra/` — bootstrap + launchd install + tailscale notes
- `scripts/` — migrations, seed data, health check

## Adding a specialist (Phase 1)

1. `mkdir agents/<name>`, subclass `shared.agent_base.AgentBase`
2. Register Slack handler: `shared.slack_dispatcher.register("<name>", handler)`
3. Add any scheduled jobs to `shared/runtime/schedule.py`
4. Never import from `agents/<other>/` — the lint plugin will catch you
5. Write a `SKILL.md`, `RUNBOOK.md`, and tests

## Logs
- Per-job: `var/log/<job>.out.log` / `.err.log`
- DB: `agent_runs` table (status, timing, tokens, cost), `audit_log` (every write)

## Phase 4 Migration Notes

Phase 4 moves this to GCP (Cloud Run + Cloud Scheduler + Cloud SQL Postgres + Secret Manager + pgvector). Every shared module is already portability-ready. Migration is a config change, not a rewrite.

**Swap procedure:**
1. `REVOPS_DB_URL=postgresql://...` → SQLAlchemy picks up Postgres; run `python -c "from shared.db.connection import init_schema; init_schema()"` against the new DB (schema.sql is dialect-safe).
2. `REVOPS_SECRETS_BACKEND=gcp_secret_manager` + `GCP_PROJECT=<id>` → `shared/secrets.py` routes to Secret Manager (requires `pip install .[gcp]`).
3. `REVOPS_KNOWLEDGE_BACKEND=pgvector` → `ChromaBackend` swaps to `PgVectorBackend` (Phase 4 deliverable — interface already frozen in `shared/mcp/knowledge_mcp.py`).
4. `REVOPS_LOG_DIR` → Cloud Run uses stdout; no code change, just don't set the var.
5. Generate Cloud Scheduler jobs from `shared/runtime/schedule.py` (see `shared/runtime/cloud_scheduler/README.md` — generator is Phase 4 work).

**Portability invariants (CI-enforced by `tests/test_portability.py`):**
- No hardcoded `/Users/ottimate/` or `/Users/jarvis/` in `shared/` or `agents/`.
- DB access only via `shared.db.connection.get_engine()`.
- Secrets only via `shared.secrets.get_secret()`.
- Cron only via `shared.runtime.schedule.SCHEDULE` (plists + Cloud Scheduler are generated).
- Slack Socket Mode (works behind Tailscale on Mac, works on Cloud Run with no inbound port — same code).

**Shortcuts to unwind in Phase 4:**
- `salesforce_mcp.py` shells out to the local `sf` CLI. GCP Cloud Run will use a JWT-auth connected app via `simple-salesforce` instead. Interface of the module stays the same; only `_sf()` changes.
- `bulk_update` is simulated in Phase 0 (`{"simulated": True}`). Phase 1 wires real bulk API calls behind the same governance gate.
- `chromadb` persistent client uses local disk. Phase 4 replaces with pgvector against Cloud SQL.

## Safety rules (Phase 0)
- No production SF writes. Smoke tests are reads only.
- `SLACK_DEV_GUARD=1` refuses to send anywhere except `SLACK_TEST_CHANNEL` (O's DM).
- `.env` is in `.gitignore`. Only `.env.example` is committed.
- Lint plugin forbids cross-agent imports.
