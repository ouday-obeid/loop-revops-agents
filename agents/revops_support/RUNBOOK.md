# RevOps Support Agent — RUNBOOK

Keep this file terse. Commands a human would actually run, in order.

---

## Prereq: O provisions the write service user

Before any prod write path is exercised:

1. Create `revops-agent@tryloop.ai` in SF — API-only or full seat per the
   license decision.
2. On the box that runs the cron:
   ```bash
   sf org login web --alias revops-agent-prod
   sf org login web --alias salesops-sandbox
   ```
3. Set in `.env` (both macOS and CI):
   ```
   SF_ORG_ALIAS=salesops
   SF_WRITE_ORG_ALIAS=revops-agent-prod
   SF_SANDBOX_ORG_ALIAS=salesops-sandbox
   ```

Until these are in place, `SF_WRITE_ORG_ALIAS` falls back to `SF_ORG_ALIAS`
and every write will attribute to O's personal session. That's fine for
sandbox but unacceptable for prod — the audit trail loses attribution.

## Wiring into the OO daemon

1. `agents/revops_support/main.py` exports `register_with_dispatcher` and
   binds both `revops_support` and `revops-support` aliases. OO calls it at
   bootstrap:
   ```python
   from agents.revops_support.main import register_with_dispatcher as revops_register
   # inside bootstrap():
   revops_register()
   ```
2. Restart the OO daemon. `@oo revops-support ping` should return `pong`.

## Launchd / cloud_scheduler wiring

The 3 revops_support jobs in `shared/runtime/schedule.py` are source of truth.
Any plist or Cloud Scheduler job should be regenerated from that list —
never hand-written.

On the Mac Mini:
```bash
cd ~/loop-revops-agents
bash infra/install_launchd.sh
launchctl list | grep com.loop-revops.revops-support
```

That script runs `python -m shared.runtime.launchd.generate` against
`shared/runtime/schedule.py`, writes plists to `var/launchd/`, copies them
to `~/Library/LaunchAgents/`, and bootstraps them.

## Pause / resume

Pause all revops_support scheduled work without stopping OO:
```bash
for p in ~/Library/LaunchAgents/com.loop-revops.revops-support-*.plist; do
  launchctl bootout "gui/$UID/$(basename "$p" .plist)" || true
done
```

Re-enable:
```bash
for p in ~/Library/LaunchAgents/com.loop-revops.revops-support-*.plist; do
  launchctl bootstrap "gui/$UID" "$p"
done
```

## Manual poll / scan

```bash
# Cooldown poller — elevates approved_primary deletes to confirmation gates.
python -c "from agents.revops_support.schema.cooldown_poller import poll; print(poll())"

# Weekly metadata snapshot — writes var/knowledge_snapshots/<YYYY-MM-DD>/sf_*.md
python -m agents.revops_support.knowledge_refresh.scheduler snapshot

# Monday digest — DMs O the diff vs canonical knowledge.
python -m agents.revops_support.knowledge_refresh.scheduler digest

# Integration health sweep.
python -c "from agents.revops_support.integration_health import flow_monitor, apex_job_monitor, metadata_drift, sync_checker; \
  [m.poll() for m in (flow_monitor, apex_job_monitor, metadata_drift, sync_checker)]"

# License audit — surfaces inactive-user tasks.
python -c "from agents.revops_support.permissions import license_audit; print(license_audit.run())"
```

## Schema change — full path

```bash
# 1) Propose — writes bundle + opens gate.
python -c "
from agents.revops_support.schema.change_proposer import propose_change
r = propose_change(
    {'action':'create','object':'Account',
     'field':{'name':'Churn_Risk__c','type':'Number','label':'Churn Risk',
              'precision':3,'scale':0,
              'description':'Rolling 30d churn risk score'}},
    justification='CS agent needs risk surfacing',
)
print(r.slug, r.approval_gate_id)
"

# 2) Sandbox — deploys to SF_SANDBOX_ORG_ALIAS with RunLocalTests, stamps manifest.
python -c "from agents.revops_support.schema.sandbox_tester import test; print(test('<slug>'))"

# 3) O approves the gate in Slack. For deletes, wait 24h then approve the
#    child confirmation gate the cooldown poller opens.

# 4) Prod deploy — pre-snapshot revert bundle + prod deploy + audit.
python -c "from agents.revops_support.schema.metadata_deployer import deploy; print(deploy('<slug>'))"

# 5) Bust describe cache for touched SObjects so next describe is fresh.
python -m agents.revops_support.query.describe_cache --bust Account
```

## Rollback a bad prod deploy

```bash
# 1) Open a rollback gate. Writes rollback_gate_id onto change.yaml.
python -c "
from agents.revops_support.schema import rollback
gid = rollback.prepare('<slug>', justification='prod breakage on <field>: <what>')
print('rollback gate', gid)
"

# 2) O approves the rollback gate in Slack.

# 3) Execute — re-deploys bundle/<slug>/revert/ to prod, audits, stamps manifest.
python -c "from agents.revops_support.schema import rollback; print(rollback.execute('<slug>'))"
```

Delete rollbacks are **not** automated in v1 — re-provisioning a deleted
field requires a fresh `sf_schema_create` bundle. The `rollback.execute()`
call will refuse with `RollbackError` if `manifest.action == 'delete'`.

## Bulk update rollback

Every bulk update writes `before_value` per chunk to `audit_log`. To revert:

```bash
# Find the audit rows for the run (group by approval_gate_id).
sqlite3 "$REVOPS_DB_URL_FILE" \
  "SELECT id, action, target, before_value FROM audit_log
   WHERE approval_gate_id = <gate_id> AND action = 'sf_bulk_update'
   ORDER BY id;"

# Re-compose the revert as a new bulk_update with fields = before snapshot.
# Open a fresh approval gate of the matching tier, then:
python -c "
from agents.revops_support.data_quality.bulk_updater import bulk_update
updates = [...]  # re-constructed from before_value rows
bulk_update('Account', updates, agent_name='revops_support', approval_gate_id=<new_gate_id>)
"
```

## Describe cache bust

```bash
# Single SObject.
python -m agents.revops_support.query.describe_cache --bust Account

# All SObjects for the active read alias.
python -m agents.revops_support.query.describe_cache --bust-all

# Weekly cleanup (>7d rows). Safe to run any time.
python -m agents.revops_support.query.describe_cache --vacuum
```

## Cooldown poller — diagnose a stuck primary

If O approved a `sf_schema_delete` primary gate but no confirmation gate
appeared after the 24h cooldown:

```bash
# See the pending primary + its cooldown.
sqlite3 "$REVOPS_DB_URL_FILE" \
  "SELECT id, action_type, status, cooldown_until
     FROM approval_gates
    WHERE action_type = 'sf_schema_delete'
      AND status = 'approved_primary'
    ORDER BY id DESC LIMIT 10;"

# Force a poll tick (safe — idempotent via NOT EXISTS child check).
python -c "from agents.revops_support.schema.cooldown_poller import poll; print(poll())"
```

Expected: one child row per primary whose `cooldown_until <= now`. If the
poll returns `[]` and cooldown has elapsed, check the launchd plist:
```bash
launchctl list | grep revops-support-cooldown-poller
```

## Failed Sunday refresh

If the Monday digest reports "no snapshot found" or the snapshot dir is
missing:

```bash
# 1) Check the snapshot root.
ls -la "$REVOPS_REPO_ROOT/var/knowledge_snapshots/"

# 2) Tail the launchd log.
tail -200 var/log/com.loop-revops.revops-support-metadata-refresh.log

# 3) Manually snapshot for today — safe, idempotent per date.
python -m agents.revops_support.knowledge_refresh.scheduler snapshot

# 4) Re-run the digest once the snapshot exists.
python -m agents.revops_support.knowledge_refresh.scheduler digest
```

If the snapshotter itself is failing, check `SF_ORG_ALIAS` login health:
```bash
sf org display --target-org salesops --json | jq '.result.connectedStatus'
```

## Dept-head lockout / escalation

Dept heads (Jackie, Hutch, Charles, Henry, Anand) do not have SF admin
access. If a dept head asks for an admin action in Slack:

1. The agent replies with the command + requires O to approve the resulting
   gate. **Never** self-approve an admin gate on behalf of a dept head.
2. If a dept head is blocked on access while O is unavailable, surface a
   `tasks` row with `priority=high` and DM O; do not open a permission_grant
   gate without O's explicit instruction.
3. `permission_grant` and `user_provisioning` tiers hardcode `approver=o_only`
   in `APPROVAL_TIERS` — enforcement is server-side, not policy.

## Duncan-style ad-hoc escalation

Historical pattern (per call transcripts): dept head DMs Duncan at 4 PM
Friday asking for a commission field, a territory update, or a permission
reshuffle. Duncan absorbs it as unbilled overflow.

This agent does not absorb overflow:

1. Ad-hoc request arrives in Slack.
2. Agent classifies and opens the matching gate with full justification.
3. O approves (or not) with full context. No unbilled cycles.
4. `reports.duncan_parity` logs the task against the week so the parity
   dashboard can compare against Duncan's invoice at retainer-review time.

When a dept head specifically names Duncan ("ask Duncan to…"), agent
responds: "I can take this — opening gate #<N> for O's review" and proceeds.

## Rate-limit breach

```bash
# Inspect current window buckets.
sqlite3 "$REVOPS_DB_URL_FILE" \
  "SELECT bucket, count, limit_value, window_start
     FROM rate_limits ORDER BY window_start DESC LIMIT 20;"
```

Hard buckets (`revops_bulk_update_daily`, `revops_metadata_deploy_daily`,
`revops_describe_calls_hourly`) raise `RateLimitExceeded`. If breached
legitimately, wait for window rollover; **do not** manually zero the row.

Soft bucket (`revops_schema_changes_weekly`) logs a WARN and continues —
no action needed, but the weekly velocity is a signal worth a debrief with O.

## Post-CEO-tier deploy verification (Week 2 Milestone 2)

Friday 3 PM ET deploy. Automation DMs O at T+30m / T+2h / T+4h with:

```
SELECT Id, DeveloperName, ParentRoleId FROM UserRole
SELECT COUNT() FROM AccountShare
SELECT COUNT() FROM OpportunityShare
```

Compare against the pre-deploy snapshot stored under
`var/knowledge_snapshots/<date>/ceo_tier_baseline.json`. Share-count delta
> ±0.01% = page O immediately.

Weekend `sync_checker` cadence auto-boosts from 30m → 2h. Monday 9 AM
automated digest with 72h deltas to O.

Rollback window is 72h. Feature flag
`revops.freeze_user_provisioning=true` stays on throughout — no new role
assignments may orphan on rollback. Rollback action = delete Anand's
UserRole row, reparent CRO to top (package staged pre-deploy).

### D10 rehearsal — sandbox validation (2026-04-13)

Full canary flow exercised end-to-end against `revagents` sandbox:

| Step | Result | Deploy ID |
|------|--------|-----------|
| `propose_ceo_role` | bundle + gate 20 opened | — |
| `sandbox_test` (NoTestRun) | Succeeded | `0AfWB00000BnpMQ0AZ` |
| `pre_snapshot` | `(none)=14, SDR=19, CSM=8, CRO=7, AE=16` | — |
| `deploy` (atomic: CEO created + CRO re-parented) | Succeeded | `0AfWB00000BnwR70AJ` |
| `schedule_verifications` | T+30m / T+2h / T+4h stamped | — |
| Immediate `verify` | passed, drift `{}` | — |
| `prepare_rollback` | gate 29 opened (`sf_schema_delete`, 24h cooldown) | — |
| `cooldown_poller` → confirm gate 30 | approved | — |
| `execute_rollback` (manifest + `--post-destructive-changes`) | CEO removed, CRO back at top | `0AfWB00000Bnyzb0AB` |

Two gotchas found + fixed in flight:

1. **sf CLI destructive changes require `--manifest`, not `--source-dir`** — `deploy_metadata` now
   switches to `--manifest package.xml` when `post_destructive_changes` is set. Previously the
   destructive XML was silently ignored.
2. **Rollback is not clean-idempotent at SF layer** — re-running `execute_rollback` after CEO is
   already gone returns `status=Failed` with componentFailure `"No Role named: CEO found"`
   (classified as Warning by SF but trips outer status=Failed). The Python flow raises. Check
   `manifest.rollback_status == 'deployed'` before re-running; the no-op guard in
   `execute_rollback` handles this but only if the manifest was stamped correctly.

### Rollback (canary-specific)

```bash
# 1) Open rollback gate.
python3 -c "
from agents.revops_support.schema import first_task_ceo_tier as c
from agents.revops_support.schema.change_proposer import _pending_dir
import yaml
bundle = _pending_dir() / 'canary-ceo-role'
m = yaml.safe_load((bundle/'change.yaml').read_text())
plan = c.CanaryPlan(slug='canary-ceo-role', path=bundle, gate_id=m['approval_gate_id'])
print('rollback gate:', c.prepare_rollback(plan, justification='<why>'))
"
# 2) Wait 24h cooldown → cooldown_poller opens confirm child gate.
# 3) Approve confirm gate in Slack.
# 4) Execute:
python3 -c "
from agents.revops_support.schema import first_task_ceo_tier as c
from agents.revops_support.schema.change_proposer import _pending_dir
import yaml
bundle = _pending_dir() / 'canary-ceo-role'
m = yaml.safe_load((bundle/'change.yaml').read_text())
plan = c.CanaryPlan(slug='canary-ceo-role', path=bundle, gate_id=m['approval_gate_id'])
print(c.execute_rollback(plan))
"
```

## Test / regression

```bash
# Unit + module-level.
cd ~/loop-revops-agents
pytest agents/revops_support/tests/ -q

# With coverage.
pytest agents/revops_support/tests/ --cov=agents.revops_support \
  --cov-report=term-missing -q

# Full regression (no revops_support fixtures leak to other agents; run last).
pytest tests/ agents/ -q
```

Expected: agents/revops_support tests green, full suite green except the
two pre-existing regressions in `agents/top_of_funnel::test_pipeline_routes_and_briefs`
and `agents/slt_metrics/tests/test_jobs.py` (tracked separately; not caused
by this agent).

## Rollback (full agent, last resort)

1. Unload the 3 launchd plists (see pause above).
2. Remove the revops_support bootstrap call from `agents/oo/main.py`, restart OO.
3. The agent's shared changes (9 new approval tiers, 4 rate-limit buckets,
   `cooldown_until` / `parent_gate_id` columns, `describe_cache` table) are
   additive. They cause no side effects when unreferenced. Leave them.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `@oo revops-support` returns "Unknown revops-support command." | Typo vs HELP_TEXT phrase match | Check `agent.py::HELP_TEXT` for exact phrase trigger. |
| `soql rejected: query must be SELECT` | Wrapping command in quotes or passing non-SELECT | Pass raw SOQL after `soql`, no quotes needed. |
| "manifest missing approval_gate_id" | `propose_change` ran but change.yaml never got the gate_id stamped | Re-run; check that `create_approval_gate` didn't raise between gate insert and manifest write. |
| "delete gate N missing approved confirmation child" | Cooldown poller didn't run, or O ignored the child gate | Run poll manually; re-approve the child; if it expired, re-issue the delete command. |
| "sandbox_test.status='failed'; expected 'passed'" on prod deploy | Schema change didn't pass sandbox RunLocalTests | Inspect `change.yaml::sandbox_test.test_failures`; fix the bundle; re-run sandbox_tester. |
| `RateLimitExceeded: revops_metadata_deploy_daily: 6/5` | >5 prod deploys today | Wait for UTC midnight rollover; these are meant to be hard-capped. |
| Describe returning stale fields after deploy | Cache not busted | `python -m agents.revops_support.query.describe_cache --bust <sobject>`. |
| Monday digest empty | No snapshot written Sunday | Tail the refresh log; manually run `scheduler snapshot`; then `scheduler digest`. |
| `audit_log.before_value` missing on bulk_update | Pre-write snapshot SOQL failed | Re-run in dry_run=True to see the error; usually a bad SOQL IN chunk when IDs contain non-id strings. |

## Go-live checklist (end of Phase 1 Week 5)

1. Flip `SF_ORG_ALIAS` from sandbox to production (if not already).
2. Verify `SLACK_DEV_GUARD` is unset for revops-support channels only.
3. Confirm `SF_WRITE_ORG_ALIAS=revops-agent-prod` is set and `sf org display
   --target-org revops-agent-prod` returns `Connected`.
4. Run `@oo revops-support pipeline by stage` — confirm live data under 10s.
5. Run the 3 launchd-bootstrapped jobs once manually to confirm each succeeds.
6. Post Milestone 5 digest to O for sign-off.
7. Start weekly `reports.duncan_parity` run every Monday to feed the Phase 3
   retainer-review decision.

---

## Phase 1.5 — Data Quality & Deal Desk

### `@admin validation monitor`
Org-wide ValidationRule health check. Pulls every active rule via Tooling API,
flags orphaned rules (formula references a `__c` field that no longer exists on
the parent object) and stale rules (`LastModifiedDate` older than 540d). Creates
a `tasks` row for Duncan per flagged rule; dedupes on title so repeat runs
don't spam.

**Manual poll:**
```
python -c "from agents.revops_support.data_quality import validation_monitor; \
  import json; print(json.dumps(validation_monitor.poll(), default=str, indent=2))"
```

**Rollback:** None — read-only. To withdraw a bad task row:
```sql
UPDATE tasks SET status = 'withdrawn' WHERE id = :id;
```

**Troubleshooting:**
| Symptom | Likely cause | Fix |
|---|---|---|
| `describe failed for <Obj>` in logs | SF CLI timeout or object without read permission on service user | Check `sf org display --target-org $SF_ORG_ALIAS`; grant FLS to the service user's profile. |
| Duplicate tasks after deploy | `tasks` table populated from a prior instance with different title format | Manually resolve old rows, then re-poll. |
| Every rule flagged as orphan | Describe returned empty `fields` — usually a sandbox without metadata access | Verify `SF_ORG_ALIAS` points at the prod read alias, not sandbox. |
