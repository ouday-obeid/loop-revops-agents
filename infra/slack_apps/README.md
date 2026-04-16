# Conductor Slack Apps — Prereq P6

Six Slack app manifests for the Conductor per-team-VM deployment model.

## V1 (2026 pilot)
- `oo_leadership.yaml` — ACTIVE. Pilot cohort: O, Hutch, Jackie, Brian, Henry, Anand.

## V1.5 fan-out (register now, enable after V1 proves out)
- `oo_sales_pod_1.yaml` — AE-focused pod
- `oo_sdr_pod_1.yaml` — SDR-focused pod
- `oo_cs_pod_1.yaml` — CSM-focused pod
- `oo_finance.yaml` — Finance + ops pod
- `oo_slt.yaml` — SLT dashboards (overlaps with oo-leadership in V1; separated for V2 role-tier splitting)

## Registration flow (O does this)
1. Open Loop AI Slack workspace admin → Apps → Manage → Build
2. Click "Create New App" → "From an app manifest"
3. Choose the Loop AI workspace, paste YAML contents, review, Create
4. Install to workspace → capture Bot Token (xoxb-...) + App-Level Token (xapp-...) + Signing Secret
5. Store per-app credentials in GCP Secret Manager (requires P4 complete) under keys:
   - `CONDUCTOR_OO_LEADERSHIP_BOT_TOKEN`
   - `CONDUCTOR_OO_LEADERSHIP_APP_TOKEN`
   - `CONDUCTOR_OO_LEADERSHIP_SIGNING_SECRET`
   - (repeat for each app with the app name substituted)
6. Add app to the relevant private channel(s); do NOT broadcast-install to all channels until consent flow is live

## Why six separate apps (not one with multi-workspace?)
Per Conductor plan file `~/.claude/plans/kind-booping-grove.md` architectural decision: "One Slack App per team VM — Socket Mode binds to one bot token; super-bot proxy adds a SPOF — defer to V2". Each VM gets its own app + token; that isolates failure domains and keeps token rotation scoped.

## Scope rationale
All six apps request identical scopes (intentional — Conductor uses the same signal collectors across audiences). If a narrower per-audience scope list is desired later (e.g. `oo-finance` doesn't need `files:write`), trim per-app at registration time.

## Socket Mode + interactivity + token rotation
- Socket Mode: ENABLED (Conductor runs on team VMs behind NAT; no inbound HTTP reachable)
- Interactivity: ENABLED (approval buttons for slt_draft_review + prompt_update_propose gates)
- Token rotation: DISABLED for V1 (manual rotation documented in RUNBOOK); consider enabling in V2

## When to revisit
Audit these manifests quarterly or after any scope addition (e.g. if we add Google Workspace integration, adjust `users:read.email`).

## Unblocks
Monday Conductor item **11745098797** prereq P6. Paired with P4 (GCP Secret Manager) for credential storage.
