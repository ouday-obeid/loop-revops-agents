# Onboarding Agent — RUNBOOK

Keep this file terse. Commands a human would actually run, in order.

---

## Wiring into the OO daemon

1. `agents/onboarding/main.py` exports both `bootstrap()` and the canonical
   `register_with_dispatcher` alias (matches the sales_reps / slt_metrics /
   revops_support pattern). OO's daemon calls it as:

   ```python
   from agents.onboarding.main import register_with_dispatcher as onboarding_register
   # inside bootstrap():
   onboarding_register()
   ```

2. Restart the OO daemon. `@oo onboarding ping` should return `pong`.

## Launchd / cloud_scheduler wiring

The 4 onboarding jobs in `shared/runtime/schedule.py` are the source of truth.
Any plist or Cloud Scheduler job should be regenerated from that list — never
hand-written.

On the Mac Mini:
```bash
cd ~/loop-revops-agents
python -m shared.runtime.launchd_gen --install
launchctl list | grep onboarding
```

## Pause / resume

Pause all onboarding scheduled work without stopping the OO daemon:
```bash
launchctl unload ~/Library/LaunchAgents/com.loop.onboarding-*.plist
```

Re-enable:
```bash
launchctl load ~/Library/LaunchAgents/com.loop.onboarding-*.plist
```

## Manual poll / scan

```bash
# Single poll tick (sandbox-only — checks SF_ORG_ALIAS)
python -c "import asyncio; from agents.onboarding.closed_won_poller import poll; print(asyncio.run(poll()))"

# Milestone scan (prints stall alert summary)
python -c "import asyncio; from agents.onboarding.milestone_monitor import scan; print(asyncio.run(scan()))"

# Location sweep
python -c "import asyncio; from agents.onboarding.location_activation import sweep; print(asyncio.run(sweep()))"
```

## Backfill preview

```
@oo onboarding backfill --preview
```
Reports the historical count of Closed Won opps missing `Onboarding__c`.
Read-only — no writes.

## Dedup reset (stall alerts)

The stall-alert dedup window is 72h. To force re-alerting for testing:
```bash
sqlite3 "$REVOPS_DB_URL_FILE" "DELETE FROM onboarding_stall_alerts;"
```
On Postgres:
```bash
psql "$REVOPS_DB_URL" -c "DELETE FROM onboarding_stall_alerts;"
```

## Rollback

1. Unload the 4 launchd plists (see pause above).
2. Remove the onboarding bootstrap call from `agents/oo/main.py`, restart OO.
3. The agent's shared changes (4 tiers + `auto_approve_gate`, 4 schedule rows)
   are additive; they cause no side effects when unreferenced. Leave them.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `@oo onboarding` returns "Specialist `onboarding` is not yet deployed" | OO daemon wasn't restarted after bootstrap wire-in | Restart OO daemon. |
| No records created despite Closed Won opps visible | Dev guard sending Slack but SF write failing | Check `audit_log.action = 'sf_create_failed'` rows. |
| Repeated duplicate `Onboarding__c` records | Belt-and-suspenders failing because `Opportunity__c` on Onboarding is null | Check the SF Flow `MT (Oppty) Closed Won Generates Onboarding` isn't also firing. |
| Stall alerts flooding channel | Dedup table missing | `python -c "from agents.onboarding.milestone_monitor import _ensure_dedup_table; _ensure_dedup_table()"` |
| Location sweep reports schema_gap | `Location__c.Activation_Status__c` was renamed | A task is auto-seeded for Agent 5. Until then, `@oo onboarding stuck-locations` returns a warning. |

## Go-live checklist (Phase 3 Week 10)

1. Flip `SF_ORG_ALIAS` from sandbox to production.
2. Confirm `SLACK_DEV_GUARD` is unset (or `SLACK_TEST_CHANNEL` cleared).
3. Run `@oo onboarding backfill --preview` against production and record count.
4. Start the poller; watch `audit_log` for the first 10 creates.
5. Verify `#cs-team` received the creation notices.
6. Flip Jackie weekly digest channel from test to `#cs-team` (env var
   `ONBOARDING_DIGEST_CHANNEL`).
