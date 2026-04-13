# Phase 0 amendment — additive PR required for Sales Reps (Agent 2)

**Scope**: additive only. Zero edits to Phase 0 public surface area (`agent_base.py`, `salesforce_mcp.py`, existing `APPROVAL_TIERS`, existing `RATE_LIMITS` buckets). Everything here is a new row.

**Why a separate PR**: Phase 0 is sign-off frozen as of 2026-04-13. These additions are required to register the seven scheduled ticks this agent owns. They are scoped narrowly so the Phase 0 team can review in <15 minutes.

**Merge order**: this PR must land before the agent is cut over from dev-guard posts to real-channel posts (Phase 3 Week 9). The agent's Slack command handler (`@oo sales-reps ...`) works without this PR; only the cron ticks require it.

---

## Diff — `shared/runtime/schedule.py`

Append seven `Job` rows to `SCHEDULE` list (after the `cs-*` block).

```python
    # Phase 1 — Agent 2 (Sales Reps). See agents/sales_reps/RUNBOOK.md.
    Job(
        name="sales-reps-grader-poll",
        cron="*/15 * * * *",
        callable_path="agents.sales_reps.scheduler.jobs:grader_poll",
        description="Every 15 min: grade new Fireflies transcripts (idempotent via storage)",
    ),
    Job(
        name="sales-reps-brief-scan",
        cron="*/15 * * * *",
        callable_path="agents.sales_reps.scheduler.jobs:brief_scan",
        description="Every 15 min: GCal demos in 90-120 min → pre-demo brief per Opp",
    ),
    Job(
        name="sales-reps-sync-check",
        cron="*/30 * * * *",
        callable_path="agents.sales_reps.scheduler.jobs:sync_check",
        description="Every 30 min: Momentum↔SF ActivityHistory diff (rate-gated alerts)",
    ),
    Job(
        name="sales-reps-risk-sweep",
        cron="0 */2 * * *",
        callable_path="agents.sales_reps.scheduler.jobs:risk_sweep",
        description="Every 2h: deal-risk sweep (pushed close / amount drop / competitor)",
    ),
    Job(
        name="sales-reps-hygiene-daily",
        cron="0 7 * * 1-5",
        callable_path="agents.sales_reps.scheduler.jobs:hygiene_daily",
        description="07:00 ET Mon-Fri: pipeline hygiene report (org-wide)",
    ),
    Job(
        name="sales-reps-leaderboard-weekly",
        cron="0 16 * * 5",
        callable_path="agents.sales_reps.scheduler.jobs:leaderboard_weekly",
        description="Friday 16:00 ET: AE + SDR leaderboard snapshot (Hutch-gated post)",
    ),
    Job(
        name="sales-reps-scorecards-weekly",
        cron="0 17 * * 5",
        callable_path="agents.sales_reps.scheduler.jobs:scorecards_weekly",
        description="Friday 17:00 ET: per-rep scorecards (Hutch-gated DMs first 4 weeks)",
    ),
```

`infra/install_launchd.sh` generates plists from this list, so the seven new jobs are picked up automatically on next bootstrap.

No changes to `APPROVAL_TIERS` — agent reuses existing `bulk_update_small`, `bulk_update_large`, and `customer_facing_comms` tiers (the last one gates coaching DMs and the weekly Hutch-confirm leaderboard button).

No changes to `RATE_LIMITS` — agent's buckets are module-local in `agents/sales_reps/rate_gates.py` (`sales_reps_grader_hourly=100`, `sales_reps_coaching_dm_daily=30`, `sales_reps_sync_alert_hourly=1`). Shared bucket `sf_bulk_update_hourly=500` covers bulk paths.

---

## Verification after apply

From repo root:
```bash
python -c "from shared.runtime.schedule import SCHEDULE; names = {j.name for j in SCHEDULE}; assert {'sales-reps-grader-poll','sales-reps-brief-scan','sales-reps-sync-check','sales-reps-risk-sweep','sales-reps-hygiene-daily','sales-reps-leaderboard-weekly','sales-reps-scorecards-weekly'} <= names, names"
python -m pytest tests/test_knowledge_and_launchd.py -q
```

Expected: assertion passes, test suite green.

## Rollback

Delete the seven rows from `shared/runtime/schedule.py`. Next run of `infra/install_launchd.sh` will remove the plists automatically.
