---
name: revops_support
description: Salesforce admin agent replacing Duncan + the unfilled RevOps Admin hire. Owns read queries, schema changes (create/modify/delete with 24h cooldown on delete), bulk data quality, user provisioning + offboarding + license audit, integration-health monitoring, and weekly SF metadata snapshots against the canonical knowledge corpus.
---

# RevOps Support Agent — SKILL

**Audience**: O — sole operator and approver. Dept heads do NOT get SF admin
access; they route requests through O.

---

## Mission (one sentence)

Replace Duncan's $15K/mo retainer + the unfilled RevOps Admin hire with a
zero-capacity-ceiling agent that has full audit attribution across every
Salesforce write, surfaces its own knowledge gaps, and routes every
high-blast-radius action through an approval gate O controls.

## Scope
- **Queries** — 8 canned SOQL reports + ad-hoc `soql <SELECT>`; 24h-cached
  `describe_sobject` to keep token cost flat.
- **Schema** — create/modify/delete `CustomField` via force-app bundle →
  sandbox deploy → O approves → prod deploy → audit + revert snapshot.
  Delete uses a 24h dual-approval cooldown (primary gate → wait → confirm
  gate) enforced server-side via `approval_gates.parent_gate_id`.
- **Data quality** — real composite-API `bulk_updater` (200 rows/chunk,
  pre-write `before_value` snapshot for rollback). Dedup, bad-conversion
  fixes, validation-rule violation sweeps.
- **Permissions** — new-hire provisioning (User + profile + perm sets +
  groups under one gate), idempotent perm-set grants, offboarding
  (deactivate + ownership transfer + package-access task), 60-day license
  audit with $70/user/mo reclaim estimate.
- **Integration health** — Flow interview failures, ApexJob failures, queue
  depth, metadata drift on the 5 watched SObjects, freshness probes on
  Vitally / Zenskar / DocuSign / Momentum / Nooks.
- **Knowledge refresh** — Sunday 02:00 metadata snapshot + Monday 09:00
  heading-level diff to O. Merge is **O-initiated** only; canonical files
  at `/Users/ottimate/sf-admin/knowledge/*.md` are never auto-overwritten.
- **Reports** — weekly Duncan-parity CSV (agent-handled vs Duncan-billed)
  wired for the Phase 3 retainer-reduction decision.

## Out of scope
- Product-side SF config (Loop Pulse, Loop Compass) — Loop Engineering owns.
- Experience Cloud / Communities — not in Loop AI stack.
- Billing platform config (Zenskar, Stripe) — agent flags, Finance executes.
- CS metrics, renewal opp creation → Agent 4 (CS).
- `Onboarding__c` auto-creation → Agent 3 (Onboarding).
- Outbound email from SF → Nooks / Marketing Cloud.
- Duncan retainer reduction execution — Phase 3 session, gated on parity
  dashboard data this agent produces.

## Slack commands

### Phase 1 Week 1 surface (read-only, live)

| Command | What it does |
|---|---|
| `@oo revops-support ping` | Health check |
| `@oo revops-support help` | Command list |
| `@oo revops-support soql <query>` | Read-only SOQL with LIMIT guard |
| `@oo revops-support pipeline by stage` | Open opps grouped by stage |
| `@oo revops-support stale opportunities [days]` | Opps stale > N days (default 30) |
| `@oo revops-support tlos with no opps` | TLOs with zero opportunities |
| `@oo revops-support opps missing products` | Closed Won without line items |
| `@oo revops-support accounts with no tlo` | Accounts missing TLO linkage |
| `@oo revops-support duplicate contacts` | Emails with >1 contact |
| `@oo revops-support active users [days]` | Users with login in last N days |
| `@oo revops-support validation rules <Object>` | Active rules for an object |

### Phase 1 weeks 2–5 surface (CLI / module entry; Slack wire-in late Week 5)

| Module | Entry | What it does |
|---|---|---|
| `schema.change_proposer` | `propose_change(intent, justification)` | Write force-app bundle + change.yaml + open gate |
| `schema.sandbox_tester` | `test(slug)` | Deploy bundle to `SF_SANDBOX_ORG_ALIAS` with RunLocalTests |
| `schema.metadata_deployer` | `deploy(slug)` | Pre-deploy revert snapshot → prod deploy → audit |
| `schema.cooldown_poller` | `poll()` | Cron-driven: elevate `approved_primary` deletes to confirm gates |
| `schema.rollback` | `prepare(slug, justification)` → `execute(slug)` | Re-deploy revert snapshot after a new gate approval |
| `data_quality.bulk_updater` | `bulk_update(sobject, updates, ...)` | Composite PATCH 200/chunk with `before_value` snapshot |
| `permissions.user_provisioner` | `provision(ProvisionRequest, ...)` | Create User + assign perm sets + groups under one gate |
| `permissions.access_grant` | `grant_permission_set`, `revoke_permission_set`, `add_to_group` | Idempotent perm-set / group wiring |
| `permissions.offboarding` | `offboard(OffboardRequest, ...)` | Deactivate + reassign ownership + surface package-access task |
| `permissions.license_audit` | `run(...)` | Surface inactive-user tasks with $/mo reclaim estimate |
| `integration_health.flow_monitor` | `poll()` | FlowInterview failures + active-obsolete Flow detection |
| `integration_health.apex_job_monitor` | `poll()` | AsyncApexJob failures + queue-depth warnings |
| `integration_health.metadata_drift` | `poll()` | Weekly describe diff on the 5 watched SObjects |
| `integration_health.sync_checker` | `poll()` | Vitally/Zenskar/DocuSign/Momentum/Nooks freshness probes |
| `knowledge_refresh.scheduler` | `run_weekly_snapshot`, `send_weekly_digest` | Sunday 02:00 / Monday 09:00 cron callables |
| `reports.duncan_parity` | `weekly(week)` | CSV: agent-handled vs Duncan-billed this week |

## Approval gates

All tiers resolved from `shared/governance.py::APPROVAL_TIERS`.

| action_type | gate style | approver | cooldown | justification |
|---|---|---|---|---|
| `sf_schema_create` | `full_workflow` | O only | — | required |
| `sf_schema_modify` | `full_workflow` | O only | — | required |
| `sf_schema_delete` | `dual_approval_cooldown` | O only | **24h** | required |
| `sf_schema_delete_confirm` | `slack_explicit` | O only | — | — |
| `user_provisioning` | `slack_explicit` | O only | — | — |
| `permission_grant` | `slack_explicit` | O only | — | required |
| `license_deactivation` | `slack_explicit` | O only | — | — |
| `bulk_update_small` (2–99) | `slack_button` | O or dept head | — | — |
| `bulk_update_large` (≥100) | `slack_explicit` | O only | — | required |

**Dual-approval cooldown semantics** (`sf_schema_delete` only):

1. Primary gate opens with `cooldown_until = now + 24h`, `status = pending`.
2. O approves → `status = approved_primary`.
3. `revops-support-cooldown-poller` (`*/15 * * * *`) picks up expired-cooldown
   primaries and opens a `sf_schema_delete_confirm` child gate linked via
   `parent_gate_id`.
4. O approves the child → `metadata_deployer.deploy()` executes the destructive
   deploy. If O ignores the child, it expires silently; primary stays as audit.

## Rate limits

From `shared/governance.py::RATE_LIMITS`.

| bucket | limit | window | mode |
|---|---|---|---|
| `revops_bulk_update_daily` | 500 | day | hard (raises) |
| `revops_schema_changes_weekly` | 10 | ISO week | **soft** (logs WARN, allows) |
| `revops_describe_calls_hourly` | 200 | hour | hard |
| `revops_metadata_deploy_daily` | 5 | day | hard |

Soft buckets surface a Slack alert via `SOFT_LIMIT_BUCKETS` but never block a
live change. The weekly schema-change budget is soft on purpose — a backlog
day shouldn't stall work, but sustained velocity is a signal.

## Scheduled jobs

Registered in `shared/runtime/schedule.py`. Regenerate launchd plists via
`bash infra/install_launchd.sh` after edits.

| cron | job | module:function |
|---|---|---|
| `0 2 * * 0` | `revops-support-metadata-refresh` | `knowledge_refresh.scheduler:run_weekly_snapshot` |
| `0 9 * * 1` | `revops-support-metadata-digest` | `knowledge_refresh.scheduler:send_weekly_digest` |
| `*/15 * * * *` | `revops-support-cooldown-poller` | `schema.cooldown_poller:poll` |

## Environment

| Var | Value | Purpose |
|---|---|---|
| `SF_ORG_ALIAS` | `salesops` | Read path (salesops@tryloop.ai) |
| `SF_WRITE_ORG_ALIAS` | `revops-agent-prod` | Prod write path (revops-agent@tryloop.ai) |
| `SF_SANDBOX_ORG_ALIAS` | `salesops-sandbox` | Developer Sandbox for schema rehearsal |
| `REVOPS_REPO_ROOT` | `$PWD` | Portable root for `pending_changes/` + snapshots |

Every `create_record` / `update_record` / `bulk_update` call passes
`intent="write"`. Schema deploy passes `intent="sandbox"` first, then
`intent="write"` for prod. Intent routing lives in
`shared/mcp/salesforce_mcp.py::_sf()`.

## Safety rails (invariant)

1. **No prod SF writes during build phase.** Sandbox + scratch only until
   Week 3 Day 15 Milestone 3. Week 2 Day 10 CEO canary is the first prod write,
   explicitly gated.
2. **Delete = dual approval + 24h cooling.** Server-side via `parent_gate_id`
   + `cooldown_until`.
3. **Never deactivate a validation rule without querying downstream impact
   first.**
4. **Every bulk write ≥10 rows snapshots `before` to `audit_log`.** Rollback
   must exist before change executes.
5. **Knowledge base integrity** — canonical `/Users/ottimate/sf-admin/knowledge/*.md`
   never auto-overwritten. Human merge only; `gap_surfacer` writes to `tasks`,
   never to knowledge files.
6. **Rate limits respected** — `revops_bulk_update_daily=500` (hard),
   `revops_schema_changes_weekly=10` (soft, alerts O).
7. **Feature flag `revops.freeze_user_provisioning=true`** during the 72h
   post-CEO-deploy window.
8. **Dev guard** (`SLACK_DEV_GUARD=1`) stays on through build — prod Slack
   sends gated to O's DM only until Milestone 3.

## Contact
- O — sole approver, sole operator.
- Duncan Sigurdsson (WhyMinded) — incumbent admin being phased out. Parity
  dashboard tracks replacement progress; retainer reduction is Phase 3.
