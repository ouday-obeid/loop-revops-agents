# Duncan (WhyMinded) Phase-Out Plan

**Status:** Draft — O to review before Week 12 cutover
**Date:** 2026-04-17
**Owner:** O
**Target cutover:** Week 12 (per Agent 5 scoping)

## Context

Duncan is Loop AI's Salesforce admin consultant via WhyMinded. He is an
external consultant, not a Loop employee or leadership. His scope is pure SF
administration: flow/apex monitoring, dedup ops, license audits, validation
rule hygiene, user provisioning, permission-set maintenance.

Commissions remain Adhyan's scope (internal) and are explicitly OUT of
Duncan's book. This plan covers only SF admin work.

## What Agent 5 (RevOps Support) now covers

Shipped in `v0.9-admin-p1.5-wave-a` + `v0.9.1-admin-hotfix`. Verified against
RevAgents sandbox on 2026-04-17 (see
`verify/phase1_residuals_2026_04_17/revops_support_sweep_*.json`).

| Duncan's prior responsibility            | Agent 5 module                                    | Status           |
|------------------------------------------|---------------------------------------------------|------------------|
| Flow + Apex run-failure monitoring       | `integration_health/flow_monitor.py` + `apex_job_monitor.py` | Shipped, 0 failures in sandbox sweep |
| Cross-integration sync freshness         | `integration_health/sync_checker.py`              | Shipped, 5 probes live |
| Metadata drift detection                 | `integration_health/metadata_drift.py`            | Shipped |
| Validation rule hygiene (active/formula) | `data_quality/validation_monitor.py`              | Shipped (ECF read gated — workaround in place) |
| Bad conversion detection                 | `data_quality/bad_conversions.py`                 | Shipped |
| Contact dedup (cluster → merge proposal) | `data_quality/dedup_contacts.py`                  | Shipped (merge needs approval gate) |
| Account dedup                            | `data_quality/dedup_accounts.py`                  | Shipped |
| License audit (>60d no login)            | `permissions/license_audit.py`                    | Shipped — 61 candidates in sandbox |
| User provisioning lifecycle              | `permissions/user_provisioner.py` + `offboarding.py` | Shipped (writes gated on approval) |
| Permset + profile access grants          | `permissions/access_grant.py`                     | Shipped |
| Knowledge refresh (object model, automations, users/roles) | `knowledge_refresh/` | Shipped — snapshots landing daily |
| SOQL helpdesk                            | `query/canned.py` + `soql_engine.py`              | Shipped |
| Metadata deploy/retrieve                 | `schema/` (Tooling API wrappers)                  | Shipped |

## Explicit carve-outs — Duncan keeps these (for now)

- **Apex code changes** — Agent 5 reads Apex job status, does not write Apex. Code changes still need Duncan or a future engineering owner.
- **Complex Flow authoring** — Agent 5 detects Flow failures but does not author new Flows. Duncan builds, Agent 5 monitors.
- **Package installs / AppExchange integrations** — install-time consultation stays with Duncan.
- **Production deployment approvals** — even metadata deploys triggered by Agent 5 go through a governance gate (two-approver for prod).

## Cutover sequence

### Phase 1 — Week 8-9: Shadow (sandbox-only)
- Agent 5 runs daily sweeps against RevAgents sandbox.
- Duncan keeps doing the same work against prod.
- Weekly compare meeting: what did Agent 5 surface vs what did Duncan surface. Track delta.
- Exit criteria: ≥90% parity on detected issues over 2 consecutive weeks.

### Phase 2 — Week 10-11: Parallel (prod read + sandbox writes)
- Agent 5 runs READ sweeps against prod (license audit, integration health, knowledge refresh, metadata drift).
- Writes (dedup merges, user provisioning, permset grants) still go through Duncan.
- Agent 5 surfaces write proposals to `#revops-approvals` Slack channel; Duncan approves and executes.
- Exit criteria: Duncan approves ≥10 Agent 5 proposals with zero false-positive merges or provisioning errors.

### Phase 3 — Week 12: Cutover (prod writes via governance)
- Agent 5 executes prod writes behind the approval-gate system (`shared.governance`).
- Duncan's engagement transitions from "daily admin" to "on-call consultant" — retained for edge cases (Apex, Flow authoring, package installs, escalations).
- Billing: drop from full retainer to hourly/as-needed.
- Exit criteria: 4 consecutive weeks with no Duncan-escalated issues.

### Phase 4 — Week 16+: Archive
- Agent 5 owns SF admin day-to-day.
- Duncan retained for Apex/Flow authoring only, or removed entirely if Loop brings SF engineering in-house.
- Decision point: O + Henry review.

## Handoff artifacts Duncan owes before cutover

- [ ] Current SF admin runbook (if one exists outside slack history)
- [ ] List of custom permission sets + what they grant (Agent 5 needs to describe these)
- [ ] List of active validation rules flagged as "known noisy" (so Agent 5 doesn't re-flag them)
- [ ] Apex class inventory + which ones Duncan owns vs Loop engineering
- [ ] Flow inventory + which ones Duncan owns vs Loop engineering
- [ ] List of integration users + their rotation schedule (keys / OAuth)
- [ ] Monthly SF DX release checklist (if Duncan runs one)

## Risks + mitigation

| Risk                                                             | Mitigation                                                     |
|------------------------------------------------------------------|----------------------------------------------------------------|
| Agent 5 misses a class of issue Duncan catches heuristically     | Keep 2-week parity window (Phase 1); escalate on drift >10%   |
| Dedup merge error nukes real customer data                       | Every merge behind dual-approval gate (`sf_merge_contacts` tier) |
| License audit flags integration user as inactive → deactivates it | Profile allow-list in `license_audit.INTEGRATION_PROFILE_*`   |
| ErrorConditionFormula Tooling-API perm never resolved            | `validation_monitor` workaround in place (skips column); works without |
| Duncan leaves before cutover                                     | Phases 1-3 are fail-safe; revert to paid-retainer arrangement with fallback SF-Consulting-Partner contact sourced now |
| Agent 5 write bug damages prod metadata                          | All metadata deploys gated on governance approval; `shared.mcp.salesforce_mcp.deploy_metadata` requires `require_approved_gate` |

## Decision points for O

1. **Confirm Week 12 target** — is this still the SLT alignment date, or has it slipped with Phase 1 residuals soak?
2. **Billing structure post-cutover** — hourly consultant vs flat retainer vs offboarding?
3. **Knowledge capture deadline** — Duncan delivers the 7 handoff artifacts by what date?
4. **Escalation policy** — if Agent 5 hits a write gate and Duncan is off-hours, does O approve or does it sit?
5. **Apex/Flow authoring plan** — does Loop bring SF engineering in-house, or does Duncan stay as authoring consultant?

## Verification against shipped code (2026-04-17)

Run `python3 -m verify.phase1_residuals_2026_04_17.revops_support_sweep` against RevAgents sandbox for:

- dedup_contacts.scan_clusters — 0 clusters (sandbox is clean)
- license_audit.run — **61 inactive users identified**
- integration_health — 0 apex failures, 5 sync probes live
- knowledge_refresh.snapshot — 3 artifact files produced
- sf_perm_audit.outbounder_access — ECF not readable, workaround in place

Artifact: `verify/phase1_residuals_2026_04_17/revops_support_sweep_*.json`
