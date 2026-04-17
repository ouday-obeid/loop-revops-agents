---
name: sales_reps
description: Agent 2 — Sales Reps. Replaces GTM Engineer #2. Automated call grading (Fireflies → classify → Haiku/Sonnet rubric → SQLite → coaching DM), pre-demo briefs 2h before every GCal demo (SF + Fireflies + Clay + Apollo), pipeline hygiene, deal-risk sweep, Momentum↔SF sync-break monitor, Friday AE/SDR leaderboard, Friday per-rep scorecard. Read-only SF in Phase 1; bulk writes routed through shared.governance. Addressable as `sales_reps` or `sales-reps`.
---

# Sales Reps Agent

Phase 1 specialist replacing GTM Engineer #2. All writes go through `shared.governance` approval gates and `shared.mcp.salesforce_mcp`. Addressable as `sales_reps` or `sales-reps`.

## Commands
- `@oo sales-reps ping` — health check
- `@oo sales-reps grade <meeting_id>` — grade a single Fireflies call on demand
- `@oo sales-reps batch-grade <from-date> <to-date>` — grade every gradable call in the window (YYYY-MM-DD; idempotent)
- `@oo sales-reps brief <opp_id|account_name>` — pre-demo brief on demand (include_blocks available programmatically)
- `@oo sales-reps hygiene [ae_email]` — pipeline hygiene report (stale activity, missing next step, past close, single-threaded)
- `@oo sales-reps leaderboard [ae|sdr] [week]` — AE or SDR leaderboard for an ISO week (default: current, kind: ae)
- `@oo sales-reps scorecard <rep_email>` — per-rep weekly scorecard dry-run
- `@oo sales-reps sync-check` — one-shot Momentum↔SF ActivityHistory diff
- `@oo sales-reps risk-sweep` — deal-risk sweep (pushed close, amount drop, champion gone, competitor mention)

## Scheduled jobs
Cron via launchd plists generated from `shared/runtime/schedule.py` (Phase 1) → Cloud Scheduler (Phase 4). Each shells to `python -m agents.sales_reps.scheduler.jobs <tick>`.

| Tick | Cadence | Purpose |
|---|---|---|
| `grader_poll` | `*/15 * * * *` | Fireflies look-back → batch-grade (idempotent) |
| `brief_scan` | `*/15 * * * *` | GCal demos starting in 90-120 min → brief per opp |
| `sync_check` | `*/30 * * * *` | Momentum↔SF ActivityHistory diff; rate-gated alerts |
| `risk_sweep` | `0 */2 * * *` | Deal-risk signal sweep |
| `hygiene_daily` | `0 7 * * 1-5` | Pipeline hygiene report (org-wide) |
| `leaderboard_weekly` | `0 16 * * 5` | Friday 16:00 ET AE + SDR snapshots |
| `scorecards_weekly` | `0 17 * * 5` | Friday 17:00 ET per-rep scorecards |

**Schedule registration lives in `shared/runtime/schedule.py` and is the one touch outside this directory** — see `_PHASE_0_AMENDMENT.md` (to be written in a separate scoped PR).

## Approval gates in use (Phase 1)
- `bulk_update_small` (2–99 records) / `bulk_update_large` (≥100) — no v1 feature triggers bulk opp writes, but the hook is in place for future additions.
- **Weekly leaderboard post** — Hutch DM Approve button before the team channel post. Tracked via `approval_gates` row for audit.
- **Coaching DM first 4 weeks** — every coaching DM routes through Hutch via `approval_gates` row with `action_type='customer_facing_comms'`. Flip config flag after week 4 to skip.

## Rate limits
Module-local buckets registered via `agents/sales_reps/rate_gates.py`:
- `sales_reps_grader_hourly` — 100 calls/hour (cost guard; $3–5/hr ceiling)
- `sales_reps_coaching_dm_daily` — 30/day (noise guard)
- `sales_reps_sync_alert_hourly` — 1/hour (prevents alert storm during real breaks)

Shared bucket `sf_bulk_update_hourly` (500) covers any bulk path.

## Storage
- **Agent-local table** in shared DB: `sales_reps_call_grades` — one row per graded meeting. Schema idempotent-created by `call_grader/storage.ensure_schema()`. Indexed on `(rep_email, graded_at)` + `(call_type, graded_at)`.
- **Shared DB** (`shared/db/loop_revops.db`): `approval_gates`, `rate_limits`, `audit_log`, `agent_runs`, `integration_health`, `tasks`.

## Call-type taxonomy
Reused from Outbounder (calibrated for Loop AI): `first_call` / `second_call` / `follow_up` / `sdr_cold_call` — plus non-gradable filter (`onboarding`, `cs`, `pilot`, `renewal`, `internal`, `headroom`). Classifier = Haiku 4.5 (`claude-haiku-4-5-20251001`); Grader = Sonnet 4.6 (`claude-sonnet-4-6`).

Rubric: 1–5 integer per section, weighted → percentage → pass/fail at 35/50/70%. Critical-item cap: section max=3 when critical missed.

## External integrations
`agents/sales_reps/integrations/` — thin module-level clients, promotable to `shared/mcp/` in a later PR without API-shape change.
- `momentum.py` — HTTP client (`/v1/calls`); `MOMENTUM_BASE_URL` override available.
- `gcal.py` — service account; `GCAL_IMPERSONATE_USER` for domain-wide delegation.
- `clay.py` — `/people/search` (decision-maker lookup) + `/people/enrich`; swallows failures.
- `web_research.py` — Apollo only in Phase 1; returns `[]` when `APOLLO_API_KEY` missing.

## Safety posture (Phase 1)
- `SLACK_DEV_GUARD=1` during build pins all Slack posts to O's DM (`U08K2UTG3G8`). Promotion to rep channels happens at Phase 3 Week 9 rollout.
- Grader runs against historical transcripts during build; production coaching DMs gated by Hutch until week 4 of Phase 3.
- Rubric weights immutable in code during build; tuning requires Hutch approval.
- Sync-break alerts rate-limited to 1/hour/integration.
