---
name: top_of_funnel
description: Agent 1 — Top of Funnel. Replaces GTM Engineer #1. Daily ICP-targeted sourcing → Apollo/Clay enrichment → 100-pt scoring → 5-layer suppression → SF Lead creation with TLO linkage → territory routing → 07:55 SDR briefings → cadence enrollment (SF, Nooks mirrors). Writes as tof-agent@tryloop.ai. Alerts on Clay-budget 80% / 100%, schema drift, sequence rate-limit hit.
---

# Top of Funnel Agent

Phase 1 specialist. All writes go through `shared.governance` approval gates and `shared.mcp.salesforce_mcp`. Addressable as `top_of_funnel` or `tof`.

## Commands
- `@oo tof ping` — health check
- `@oo tof enrich <domain>` — run the full pipeline on one account (audit, no SDR briefing)
- `@oo tof score <domain>` — ICP score only, explanation included, no SF writes
- `@oo tof daily [dry-run]` — trigger or preview the 07:55 SDR briefing
- `@oo tof suppress <email> [reason]` — add to local suppression cache (DM-only)
- `@oo tof queue status` — pending outbound-sequence approval gates
- `@oo tof queue approve <gate_id>` — approve today's enrollment queue
- `@oo tof credits` — Clay credit usage this month

## Scheduled jobs
- `0 2 * * 1-5` — enrichment pipeline (~200 leads/day, Mon–Fri 02:00)
- `55 7 * * 1-5` — SDR daily briefing (Mon–Fri 07:55)

## Approval gates
- `bulk_update_small` (2–99 leads) / `bulk_update_large` (≥100, justification required) — ONE gate per pipeline run, passed to every `create_record`
- `outbound_sequence` — required before `sequence_enroller.enroll_batch`; 08:00 daily review window
- `suppression_override` — Slack button, O or dept head, justification required (added to `APPROVAL_TIERS` via Phase 0 amendment)

## Rate limits
- `sf_lead_creation_daily` = 200
- `nooks_sequences_daily` = 50 (51st raises `RateLimitExceeded` + audit row)

## Access control
- O can run any command (default).
- Dept heads listed under `territory.yaml:dept_heads` (Hutch, Charles) resolve via `routing.is_dept_head(email)` — consumers add this check when wrapping `@oo tof` commands that would otherwise be O-only.

## Storage
- Agent-local SQLite at `agents/top_of_funnel/state.db` — `clay_credit_ledger`, `suppression_cache`, `tof_enrichment_runs`, `tof_lead_candidates`, `tof_routing_state`, `tof_sf_user_cache` (24h TTL), `tof_sequence_enrollments`, `apollo_query_cache`
- Shared DB (`shared/db/loop_revops.db`) — `approval_gates`, `rate_limits`, `audit_log`, `agent_runs`

## Config
- `config/icp_weights.yaml` — 100-pt model (Ownership 25 / Locations 25 / Vertical 20 / Growth 20 / Tech-stack 10 + product-attach +5)
- `config/icp_config.yaml` — tier bands (A ≥70, B ≥45, exploration ≥25)
- `config/territory.yaml` — ENT / MM / SMB SDR rotations; `slack_id`/`sf_user_id` populated at D1
