# Sales Reps Agent Runbook

## Deploy
Phase 0 bootstrap must be green first.
```bash
cd $REVOPS_REPO_ROOT
source .venv/bin/activate
python -c "from agents.sales_reps.call_grader.storage import ensure_schema; ensure_schema()"  # one-time: create sales_reps_call_grades
# Register scheduled jobs in shared/runtime/schedule.py (Phase 0 amendment PR, separate scope), then:
bash infra/install_launchd.sh
```

## Register with OO dispatcher
`agents/sales_reps/main.py:register_with_dispatcher` is called by `agents/oo/main.py` at startup (both `sales_reps` and `sales-reps` aliases). If OO is already running, restart the daemon:
```bash
launchctl kickstart -k gui/$UID/com.loop-revops.oo-daemon
```

## Pause
```bash
# Pause all sales_reps scheduled jobs:
for job in grader-poll brief-scan hygiene-daily sync-check risk-sweep leaderboard-weekly scorecards-weekly; do
    launchctl bootout gui/$UID/com.loop-revops.sales-reps-$job 2>/dev/null || true
done
```
This halts cron but leaves Slack `@oo sales-reps ...` handlers live. To fully sever, comment the `register_with_dispatcher()` call in `main.py` and restart OO.

## Rollback
1. Pause (above).
2. Drop plists: `rm ~/Library/LaunchAgents/com.loop-revops.sales-reps-*.plist`
3. Remove cron entries from `shared/runtime/schedule.py` (revert the Phase 0 amendment).
4. Optional wipe of agent state: `sqlite3 shared/db/loop_revops.db 'DROP TABLE sales_reps_call_grades'` (destroys grade history; audit trail in shared DB remains).

## Logs
- Cron stdout/stderr: `$REVOPS_REPO_ROOT/var/log/sales-reps-*.{out,err}.log`
- Slack dev channel: `#agent-sales-reps-log`
- DB: `audit_log` (every SF write, every capability invocation), `agent_runs` (every run), `sales_reps_call_grades` (per-meeting grade history)

## Force a run now
```bash
cd $REVOPS_REPO_ROOT && source .venv/bin/activate

# Grade a single call on demand:
python -c "import asyncio; from agents.sales_reps.call_grader import grader; print(asyncio.run(grader.grade_one('MEETING_ID_HERE')))"

# Batch grade a date range (idempotent — skips already-graded):
python -c "import asyncio; from agents.sales_reps.call_grader import batch; print(asyncio.run(batch.grade_range('2026-04-01', '2026-04-13')))"

# Pre-demo brief for an Opp ID or account name:
python -c "import asyncio; from agents.sales_reps.pre_demo import brief_generator; print(asyncio.run(brief_generator.generate('006ABC')))"

# Sync-check tick:
python -m agents.sales_reps.scheduler.jobs sync_check --json

# All ticks support --json:
python -m agents.sales_reps.scheduler.jobs grader_poll --json
python -m agents.sales_reps.scheduler.jobs brief_scan --json
python -m agents.sales_reps.scheduler.jobs hygiene_daily --json
python -m agents.sales_reps.scheduler.jobs risk_sweep --json
python -m agents.sales_reps.scheduler.jobs leaderboard_weekly --json
python -m agents.sales_reps.scheduler.jobs scorecards_weekly --json
```

## Threshold tuning
- **Rubric weights** — immutable in code during Phase 1 build. Tuning requires Hutch approval; edits land in `call_grader/rubrics.py`.
- **Pass/fail thresholds** — `call_grader/rubrics.py` — 35% / 50% / 70%. Hard-coded; same approval requirement.
- **Grader cost ceiling** — `sales_reps_grader_hourly = 100` in `agents/sales_reps/rate_gates.py`. Raise with cost review.
- **Sync-break grace window** — `GRACE_MINUTES = 15` and `TIME_MATCH_WINDOW_MIN = 5` in `momentum_sync_monitor.py`. Widen grace if Momentum→SF sync routinely takes >15 min.
- **Demo lookahead** — `min_lookahead_minutes=90`, `lookahead_minutes=120` in `pre_demo/trigger.py:scan_upcoming`. Keep 30-min overlap with tick cadence so no demo slips.
- **Scorecard trend threshold** — `±2 pct` dead zone for trend arrow; `scorecards.py:_render`.

## Weekly Hutch gate (Phase 1 only)
The `leaderboard_weekly` and `scorecards_weekly` ticks *compute* payloads but do NOT post. Posting goes through a Hutch approval gate:

1. Tick writes payload to `approval_gates` with `action_type='customer_facing_comms'`, `target='leaderboard:weekly'` or `target='scorecards:weekly'`.
2. Slack dispatcher posts "Approve leaderboard for {week}?" Block Kit button to Hutch DM.
3. On click, `require_approved_gate` is consumed and the real post fires.
4. Flip `config/weekly_gate.enabled=false` at week 4 of Phase 3 rollout to skip the gate.

## Coaching DM gate (first 4 weeks of Phase 3)
- Every grader run that produces critical-miss coaching DM queues an `approval_gates` row.
- Hutch reviews daily → approves → scheduler dispatches DM to rep.
- Audit row written on both queue and dispatch.

## Handling sync-break storm
`sales_reps_sync_alert_hourly` bucket is 1/hour. When the bucket is exhausted:
1. Alert suppresses silently (no Slack post) but writes an audit row.
2. `run_once` still returns the full break list — caller can log it for post-hoc analysis.
3. Reset the bucket manually only if you're actively debugging: `DELETE FROM rate_limits WHERE bucket = 'sales_reps_sync_alert_hourly'`.

## Handling grader rate-limit hit
`sales_reps_grader_hourly = 100`. On hit:
1. `batch.grade_range` sets `rate_limited_stopped_at = <meeting_id>` and returns.
2. Next `grader_poll` tick picks up where we left off (idempotent via `storage.grade_exists`).
3. If this recurs 3+ ticks in a row, bump the bucket with O approval — cost implication ~$3–5 per 100 grades.

## False-positive playbook
Hutch (or a rep) flags a grade as wrong:
1. Open the grade row: `sqlite3 shared/db/loop_revops.db 'SELECT * FROM sales_reps_call_grades WHERE meeting_id = "MEETING_ID"'`.
2. If the rubric misread (LLM drift): flag to Hutch for rubric review; do NOT silently re-grade.
3. If transcript was corrupted: delete the row and re-run `grader.grade_one(meeting_id)`.
4. If classifier mislabeled the call type: raise in `#agent-sales-reps-log`; classifier changes need a regression set.

## Reset a rep's coaching state
Per-rep coaching state is the rep's history in `sales_reps_call_grades` — scorecards, trend arrows, and coaching DMs all derive from this table keyed on `rep_email`. Reset scenarios: rep switches teams, dispute requires clean slate, or rubric change invalidates prior grades.

```bash
REP=rep@tryloop.ai

# 1. Inspect what will change:
sqlite3 shared/db/loop_revops.db "SELECT meeting_id, call_type, percentage, graded_at FROM sales_reps_call_grades WHERE rep_email = '$REP' ORDER BY graded_at DESC"

# 2. Archive to a side table (preferred over destructive delete — keeps audit trail):
sqlite3 shared/db/loop_revops.db "CREATE TABLE IF NOT EXISTS sales_reps_call_grades_archive AS SELECT * FROM sales_reps_call_grades WHERE 0"
sqlite3 shared/db/loop_revops.db "INSERT INTO sales_reps_call_grades_archive SELECT * FROM sales_reps_call_grades WHERE rep_email = '$REP'"
sqlite3 shared/db/loop_revops.db "DELETE FROM sales_reps_call_grades WHERE rep_email = '$REP'"

# 3. Clear the rep's pending coaching-DM approval gates (avoids stale DMs firing post-reset):
sqlite3 shared/db/loop_revops.db "UPDATE approval_gates SET status = 'cancelled' WHERE action_type = 'customer_facing_comms' AND target LIKE 'coaching_dm:$REP%' AND status = 'pending'"

# 4. Write an audit row so the reset itself is traceable:
python -c "from shared import governance; governance.write_audit(agent_name='sales_reps', action='coaching_state_reset', target='rep:$REP', after={'archived_rows': True})"
```

Scorecards recompute from scratch on the next `scorecards_weekly` tick — no further action needed.

## Rotate Fireflies API key
The grader, pre-demo brief, and integration health probe all resolve `FIREFLIES_API_KEY` through `shared.secrets.require_secret` at call time — no process-level caching. Rotation is a file-edit plus daemon restart:

```bash
# 1. Edit .env with the new key (use VS Code so it stays out of shell history):
code $REVOPS_REPO_ROOT/.env

# 2. Confirm the change is readable:
python -c "from shared.secrets import require_secret; print('ok' if require_secret('FIREFLIES_API_KEY').startswith('ey') else 'WRONG')"

# 3. Smoke-test against the live API before the next tick fires:
python -m shared.mcp.fireflies_mcp --smoke

# 4. Restart the OO daemon so long-running in-process callers (Socket Mode listener,
#    scheduled ticks whose modules were already imported) pick up the new value:
launchctl kickstart -k gui/$UID/com.loop-revops.oo-daemon

# 5. Revoke the old key in Fireflies admin → API Keys, AFTER steps 2–4 succeed.
```

If the key rotates during an in-flight `grader_poll` or `batch.grade_range`, Fireflies returns 401 and the run writes a degraded audit row — the next tick picks up from the last graded meeting (idempotent via `storage.grade_exists`).

## Incident: Momentum API down
1. `momentum_sync_monitor.run_once` returns `{"error": "..."}` and does NOT consume the alert bucket.
2. Hygiene runs continue unaffected.
3. Check `integration_health` table; if down > 2h, DM O.

## Known risks
- **Momentum↔SF sync monitor false positives.** Two-stage probe (CallObject ID → time+rep+contact window) handles most tenants, but a tenant without Task.CallObject and with heavily-truncated Momentum rep emails could miss matches. Grace window is 15 min — widen if real syncs regularly run longer.
- **Classifier drift.** Haiku 4.5 is the classifier; if Anthropic updates Haiku and label distribution shifts, rubric averages will drift. Regression set lives in `tests/fixtures/` (10 real transcripts) — re-run on model change.
- **Coaching DM Phase 3 scale-up.** 30/day bucket is sized for ~10 reps × 3 critical misses/day. At scale (30 reps), raise to 100/day with O sign-off.
- **Pre-demo brief false negatives.** Title regex filters in `pre_demo/trigger.py`. Edge cases (e.g., "Acme strategy session") slip through. Add to `_DEMO_TITLE_PATTERN` as they're reported; rerun regression tests.
- **Calendar race.** If a demo is rescheduled inside the 2h window after the brief fires, rep gets the old brief. No post-brief refresh in Phase 1; add in Phase 2 if reports warrant.
- **`revops-agent@tryloop.ai` SF user (Phase 2+).** SalesRepsAgent runs `sf_service_user=None` in Phase 1 (read-only). If/when bulk writes are added, create the user with narrow scope first.
- **Fireflies delay.** Transcripts land in Fireflies 5–15 min after the call ends. `grader_poll` look-back is 20 min; if Fireflies falls further behind, miss will show up as lower-than-expected graded counts — extend look-back in `scheduler/jobs.py:grader_poll`.

## Escalation
- Non-urgent: post to `#agent-sales-reps-log`
- Urgent (grader cost spike, sync-break storm, false coaching DM): DM O at `U08K2UTG3G8`
- Dept-head access: Hutch (VP Sales, full `sales_reps` access), Charles (ENT), Nate (MM)
