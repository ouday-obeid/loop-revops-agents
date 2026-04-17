# Scenario 1 Readiness Assessment — 2026-04-17

**Scenario 1:** Lead → qualified → demo booked
**Baseline:** main @ `8c79255` (two chore commits past `v0.9.1-admin-hotfix`)
**Tests:** 1,707 collected, 1,700 passed + 7 skipped (0 failures)

## Readiness checks

### ✅ ToF + Sales Reps + OO dispatch
- Dispatch round-trip artifact: `verify/phase1_residuals_2026_04_17/dispatch_roundtrip_20260417T191814Z.json`
- All 6 specialists registered on shared dispatcher (oo, sales_reps, revops_support, slt_metrics, top_of_funnel, cs, onboarding)
- 20/20 ping + help probes green
- 6/6 persona aliases (outbounder, closer, onboarder, supporter, admin, urkel) route to correct targets
- Sandbox pipeline_hygiene ran end-to-end (7 findings across 4 AEs) — the Stuck parent is un-stuck

### ✅ Governance approval button wiring
- `tests/test_governance.py` — 30 tests pass
- `tests/test_slack_dispatcher.py` — 6 tests pass
- `tests/scenarios/test_scenario_1_lead_to_demo.py` — 3 tests pass (the harness that Scenario 1 actually flows through)
- `shared/slack_dispatcher.handle_gate_decision` includes the cooldown-guard for dual-approval flows (shipped with v0.9-admin-p1.5-wave-a and still green in v0.9.1)
- `shared/governance.require_approved_gate` refuses un-approved gates; `auto_approve_gate` is restricted to system callers with audit trail

### ⚠️ Hutch dept-head access — Bucket C open
- Hutch has not confirmed he can DM `@oo sales-reps leaderboard` or `@oo tof score <domain>` from his own DM. Draft ask staged at `verify/phase1_residuals_2026_04_17/bucket_c_slack_drafts.md` — send pending O approval.
- Scenario 1's SDR→AE handoff step expects Hutch's SDR/AE leaderboard dispatch to work from his DM. Wiring is proven in tests; the human-level acceptance check is outstanding.

## Verdict

**Scenario 1 is technically ready to fire** — all code paths green, all integration tests pass, governance approval flow wired and tested.

**But it should NOT fire until Hutch's Bucket C confirmation lands.** Running Scenario 1 without confirming Hutch can actually drive the AE side in production would risk embarrassment if he hits an unresolved wiring issue live.

## Recommended sequence

1. **Today or Friday:** O approves Hutch's Slack ask (see `bucket_c_slack_drafts.md` §1). Send it.
2. **When Hutch replies green:** fire Scenario 1 in the staging channel, not #l-revenue. Capture the full timeline artifact.
3. **If Scenario 1 clean:** propose Phase 3 Week 7 prod rollout plan to SLT.
4. **If Scenario 1 surfaces issues:** they're bugs against the wiring we just verified — file issues, fix before re-fire.

## Non-blockers identified this session

- Fireflies MCP adapter returns HTTP 404 on `/graphql/` — endpoint has moved or key rotated. Blocks call grader. Not a Scenario 1 blocker (Scenario 1 doesn't grade calls).
- RevAgents sandbox schema drift from prod: `Account.AnnualRevenue` FLS-gated, `Opportunity.Segment__c` / `ICP_Score__c` / `Count_Balance__c` missing, `Task.Source__c` missing, `Top_Level_Organization__c.Domain__c` missing. Blocks pipeline_fetcher + icp_scorer full coverage + sync_checker Momentum/Nooks probes. Not a Scenario 1 blocker — Scenario 1 runs on synthetic fixtures in `tests/scenarios/`, not against sandbox schema.
- 9 `.env` placeholders remain REPLACE (FIREFLIES, VITALLY, ANTHROPIC, GOOGLE_SERVICE_ACCOUNT_JSON, CLAY, APOLLO, MOMENTUM, JACKIE_SLACK_USER_ID, GCAL_IMPERSONATE_USER). Each blocks the corresponding capability in prod. Not Scenario 1 blockers.
