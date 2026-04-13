---
name: onboarding
description: Auto-creates Onboarding__c on SF Closed Won (≤5 min), monitors stall + null-OwnerId + stuck-location signals, runs Sales→Impl→CS handoff checklist, weekly Jackie digest.
---

# Onboarding Agent — SKILL

**Audience**: Jackie (Director of CS) — dept-head access. O — full override.

---

## Mission (one sentence)

Every Salesforce Opportunity that transitions to `StageName = 'Closed Won'`
results in a single, correctly-populated `Onboarding__c` record within ≤5
minutes, with CSM enforcement, milestone stall detection, location activation
tracking, and a verifiable Sales → Implementation → CS handoff checklist.

## Scope
- Auto-create `Onboarding__c` from Closed Won opps (idempotent; ≤5-min latency).
- Monitor two stage fields — `JK_Onboarding_Stage__c` (Jackie's picklist) and
  `Overall_Onboarding_Status__c`. Flag stalls ≥5 business days on BOTH.
- Track location activation via `Location__c.Activation_Status__c` / `Stuck_Reason__c` when present; if absent, log `schema_gap`, auto-seed an Agent 5 task, and skip the sweep until the fields are added.
- Catch `Onboarding__c` with null `OwnerId` and post a CSM-reassignment approval.
- Run a 6-item Sales → Implementation → CS handoff checklist on demand.
- Weekly digest to Jackie every Friday 9 AM ET.

## Out of scope
- Churn risk / health scores / renewals → Agent 4 (CS).
- Any schema add or modify (e.g., adding `Onboarding_Record_Created__c`) →
  Agent 5 (RevOps Support). This agent seeds a task and waits.
- Self-serve product provisioning → Sundar's system (stub until spec lands).
- Customer-facing kickoff emails → not this agent.

## Slack commands

| Command | What it does |
|---|---|
| `@oo onboarding ping` | Health check |
| `@oo onboarding status <account>` | Onboarding__c snapshot for an account |
| `@oo onboarding stalls [days]` | Stalled onboardings (default ≥5 business days) |
| `@oo onboarding unassigned` | Onboardings with null OwnerId |
| `@oo onboarding stuck-locations [account]` | Stuck locations, optionally filtered |
| `@oo onboarding handoff <account>` | Run handoff checklist on demand |
| `@oo onboarding backfill --preview` | Historical Closed Won without Onboarding__c (read-only) |
| `@oo onboarding help` | Command list |

## Approval gates
- **`onboarding_auto_create`** — auto-approved. Every poll-time create writes
  this gate row with `origin` + `opportunity_id` in payload for audit.
  Runtime gate passed to the SF MCP uses the `single_record_update` tier to
  satisfy the strict action_type check; business intent is preserved in the
  payload.
- **`csm_reassignment`** — Jackie or O via Slack button. One per unassigned
  onboarding.
- **`onboarding_complete`** — Jackie or O via Slack button when an onboarding
  moves to Completed manually.
- **`skip_milestone`** — Jackie or O, slack_explicit with required
  justification. Used to override a blocking handoff-checklist item.

## Scheduled jobs

| cron | job | module |
|---|---|---|
| `*/5 * * * *` | closed-won poller | `closed_won_poller:poll` |
| `0 */6 * * *` | milestone monitor | `milestone_monitor:scan` |
| `0 9 * * *` | daily location sweep | `location_activation:sweep` |
| `0 9 * * 5` | Friday Jackie digest | `dispatcher:send_jackie_weekly_digest` |

## Rate limits + safety
- Dev guard (`SLACK_DEV_GUARD=1`) stays on through Week 10; all Slack posts
  route to `SLACK_TEST_CHANNEL` until rollout.
- Sandbox only until Phase 3 Week 10 rollout. Flip is `SF_ORG_ALIAS` only —
  no code change required.
- Poller batch cap = 50 per tick (configurable). One failing opp does not
  abort the batch; the error is audited and the next opp proceeds.

## Contact
- Jackie Kroeger-Donovan — Director of CS, primary dept-head approver.
- O — override authority on any gate.
