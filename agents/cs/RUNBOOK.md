# CS Agent Runbook

## Deploy

CS has no daemon of its own — it registers as a handler on the shared Slack dispatcher owned by OO. To bring CS online:

1. Ensure Phase 0 infra is deployed (`bash infra/bootstrap.sh`, `bash scripts/run_migrations.sh`).
2. Confirm OO's `bootstrap()` calls `agents.cs.main.bootstrap()` after registering itself. See Wiring below.
3. Install the launchd plists generated from `shared/runtime/schedule.py` (includes `cs-*` jobs once registered there).

### Wiring into OO's daemon (one-time at Phase 1)

`agents/oo/main.py::bootstrap()` must also register CS. Add:

```python
from agents.cs import main as cs_main
cs_main.bootstrap()
```

placed after `register("oo", oo_dispatcher.handle)`. This is a cross-agent change OO owns — do not edit it from the CS side.

## Pause

```bash
launchctl bootout gui/$UID/com.loop-revops.cs-health-poll
launchctl bootout gui/$UID/com.loop-revops.cs-churn-sweep
launchctl bootout gui/$UID/com.loop-revops.cs-renewal-pipeline
launchctl bootout gui/$UID/com.loop-revops.cs-renewal-stall
launchctl bootout gui/$UID/com.loop-revops.cs-expansion-scan
launchctl bootout gui/$UID/com.loop-revops.cs-weekly-report
launchctl bootout gui/$UID/com.loop-revops.cs-integration-health
```

Pausing CS jobs does not affect the OO daemon — `@oo cs <cmd>` still resolves to the registered dispatcher (which returns stale data if pollers are paused).

## Rollback

All CS SF writes are governance-gated. To fully disable:

1. Pause (above).
2. Remove CS's registration: restart the OO daemon after deleting `cs_main.bootstrap()` call from `agents/oo/main.py`.
3. Revert migrations for `cs_account_health`, `cs_churn_risk`, `cs_renewal_state` via alembic.

## Logs

- Per-job stdout/stderr: `$REVOPS_REPO_ROOT/var/log/cs-<job>.out.log` / `.err.log`
- DB-backed: `agent_runs` filtered by `agent_name='cs'`, `audit_log` filtered by `agent_name='cs'`, `integration_health` for Vitally/Fireflies/SF/Momentum probes.

## Force a job now

```bash
cd $REVOPS_REPO_ROOT && source .venv/bin/activate
# Health poll
python -c "import asyncio; from agents.cs.health.health_monitor import poll; asyncio.run(poll())"
# CS integration health
python -c "import asyncio; from agents.cs.integration_health import poll; asyncio.run(poll())"
# Churn sweep
python -c "import asyncio; from agents.cs.risk.churn_risk import run_sweep; asyncio.run(run_sweep())"
# Weekly report
python -c "import asyncio; from agents.cs.reports.weekly import send; asyncio.run(send())"
```

### CS integration health probes

The CS-specific poller (`agents/cs/integration_health.py`, every 30m) writes
`cs_*`-prefixed rows to `integration_health` — distinct from OO's unprefixed
generic probes. Status transitions to `degraded`/`down` auto-open idempotent
`revops_support` tasks (source `cs:integration_health:<integration>`).

- `cs_vitally` — live `list_accounts(limit=1)` round-trip
- `cs_fireflies` — live `list_transcripts(limit=1)` round-trip
- `cs_salesforce` — `SELECT COUNT(Id) FROM Account`
- `cs_momentum_sync` — ≥1 Momentum-sourced Task in last 7d (silent-break detector)
- `cs_nps_freshness` — ≥40% of `cs_account_health` rows with `nps_at` within 30d

## Threshold tuning

Churn risk thresholds (50 / 70 / 85) live in `agents/cs/risk/scoring.py` at module-level constants. To adjust:

1. Edit the constants.
2. Regenerate last 30d of scores dry-run (`--dry-run` flag on `run_sweep`) to see alert delta.
3. Post delta report to #agent-cs-log for Jackie review before committing.

## False-positive playbook

When Jackie/Blaine flag a tier ≥70 alert as a false positive:

1. Query `cs_churn_risk.factors_json` for that account to see which factors drove the score.
2. If a specific factor consistently misfires (e.g., NPS unknown scoring too high), open a PR adjusting weight or normalization rule in `risk/scoring.py`.
3. Do not mute accounts ad-hoc — fix the signal or the formula.

## Approval gate flows (M9)

All gate creation + side effects live in `agents/cs/handlers/slack_actions.py`. The generic approve/reject button handler in `shared/slack_dispatcher.py` flips `approval_gates.status`; the `finalize_*` functions here run the downstream write.

### CSM reassignment
- `request_csm_reassignment(account_id, old_owner_id, new_owner_id, reason, slack_sender)` → Gate `csm_reassignment`.
- Jackie approves → `finalize_csm_reassignment(gate_id, approver)` writes `Account.OwnerId`.

### Churn-prevention outreach
- `request_churn_outreach(account_id, csm_slack_id, draft_markdown, reason, slack_sender)` → Gate `cs_churn_outreach`.
- Jackie approves → `finalize_churn_outreach(gate_id, approver)` DMs the approved draft to the CSM. **No customer-facing write.**

### Mark Churned (dual approval)
1. `request_mark_churned(account_id, justification)` → Gate A (`mark_churned_request`), Jackie-approved.
2. On A approve, call `on_mark_churned_primary_approved(a_id)` to auto-create Gate B (`mark_churned_confirm`) with `parent_gate_id = A.id`. Idempotent — if B already exists it is returned.
3. O approves Gate B → `finalize_mark_churned(b_id, approver)` verifies:
   - B status = approved, action = `mark_churned_confirm`
   - parent gate A exists, is `mark_churned_request`, status = approved
   - `payload.account_id` matches between A and B
4. On pass, writes `Account.Churn_Status__c = 'Churned'` with both gate IDs in `audit_log`.

Rollbacks: if Gate A is rolled back out-of-band between B-creation and B-finalize, the SF write refuses.

### Bulk CSM reassignment

If multiple accounts need reassignment (CSM rotation, PTO coverage):

1. Prepare CSV `account_id,new_csm_user_id`.
2. Loop through, call `request_csm_reassignment()` per row. Jackie approves each via the Slack buttons; `bulk_update_large` tier is reserved for non-owner fields and is not used here.

## Known risks

- **False-positive churn alerts** erode trust — tier 50 is log-only, tier ≥70 requires ≥2 non-zero factors.
- **Vitally UID mismatch** — deterministic resolver only; misses become RevOps Support tasks. Weekly report surfaces unresolved count.
- **SF rate limits** — `check_rate_limit('sf_bulk')` guards any >25-row op.
- **Slack alert fatigue** — one scoring row per account per day (enforced by UNIQUE constraint).

## Escalation contacts

- Blaine Alleluia (CS Ops) — primary alert recipient
- Jackie Kroeger-Donovan (Director of CS) — CC on tier ≥70, primary approver for CSM change / outreach / Mark Churned (Jackie side)
- O — dual approver for Mark Churned
