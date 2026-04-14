# Agent 7 — Conductor (Evolved OO)

Scoping doc. Locks architecture before implementation. Sits above the six Phase 1 specialists (Top of Funnel, Sales Reps, Onboarding, CS, RevOps Support, SLT Metrics) and project-manages them on behalf of each GTM individual at Loop.

## Premise

The current OO agent is a single-operator dispatcher. Agent 7 evolves OO into a **per-individual, role-aware conductor** that:

1. Knows each rep personally (persistent memory + inferred habits)
2. Watches their full tech stack (Slack, Gmail, Calendar, Salesforce, Fireflies) passively
3. Proactively nudges them through Slack DM based on accumulated understanding
4. Delegates downstream work to the six specialists
5. Contributes anonymized learnings to a central hub that self-improves the system

This is not a new agent. It is OO + memory + telemetry + fleet + self-improvement.

## Fleet topology

**One VM per team, not per user.** Estimated fleet size: ~10–14 VMs.

| Team tier | Count | Notes |
|---|---|---|
| Sales pods | 4 | MM + ENT splits per Hutch/Charles structure |
| SDR pods | 4 | Aligned to sales pods |
| CS pods | 2–3 | Per Jackie's team split |
| Finance | 1 | |
| SLT | 1 | O + executives |
| Central hub | 1 | Aggregation + self-improvement |

Each team VM runs a full clone of the 6-specialist system + the Agent 7 conductor. Within a VM, per-user isolation is **partition-based, not process-based** — one process, many user memory partitions keyed by Slack user ID.

**Why VM-per-team (not per-user):** 30+ VMs is an ops nightmare; 1 VM is a privacy/blast-radius problem. Team-level matches the natural trust boundary — SDRs on the same pod already share pipeline context, managers already see their team's activity. Privacy at the team boundary is the same as privacy in real life.

**Central hub:** dedicated VM. Receives scrubbed telemetry (see Privacy contract below), runs the self-improvement cron, pushes skill/prompt updates back down to team VMs. No raw user data crosses into the hub.

## Identity & roles

Each user has:
- **Slack user ID** — primary key
- **Role** — SDR, AE, CSM, Onboarding Manager, Finance, CRO, CEO, etc. Drives the default persona/surface.
- **Team VM assignment** — which VM handles them
- **Personal wiki** — the memory surface (see below)
- **Consent record** — what they've authorized (see Onboarding)

Role provides the *template*; the personal wiki accumulates the *individual*.

## Consent & onboarding flow

First interaction triggers a consent onboarding (Slack DM conversation):

1. Agent introduces itself: "I'm the Loop RevOps conductor. To be useful to you I need to observe your work surfaces. Here's what I'd read and why."
2. Per-surface consent checklist, each opt-in:
   - Gmail (via Google OAuth) — read sent/received for follow-up tracking
   - Google Calendar — read meetings for context
   - Slack — read DMs/channels the bot is already in (no additional scope)
   - Salesforce — already governed via service user; no per-user consent needed
   - Fireflies — read transcripts of calls they're on
3. Rep completes OAuth flows; tokens stored per-user, encrypted, scoped to that team VM.
4. Consent record persisted; revocable at any time via `/oo revoke`.
5. HR/legal disclosure text appears in the onboarding DM (to be drafted with Loop People Ops before V1 ships).

**Non-negotiable:** no silent watching. If a user hasn't onboarded, the agent does not read their surfaces.

## Signal surface

Full-stack rolling telemetry, per consented user:

| Source | What's collected | Refresh |
|---|---|---|
| Slack | DMs to bot, @-mentions, channel messages where user is active | Event-driven |
| Gmail | Sent/received metadata + subject + body | 5 min poll |
| Google Calendar | Events + attendees + titles | 15 min poll |
| Salesforce | Owned Opps/Accounts stage + activity + close date changes | Event via platform events |
| Fireflies | Transcripts of calls where user attended | Post-meeting webhook |
| Momentum | Call intel summaries | Fireflies-equivalent |

All signals write to a **per-user rolling event log** on the team VM. Retention: 90 days raw, indefinite summaries.

## Privacy contract (team VM → central hub)

The hub only receives **scrubbed, aggregated** data. Specifically:

| Layer | Crosses to hub? | Notes |
|---|---|---|
| Raw Slack/Gmail/Fireflies text | Never | Stays on team VM |
| Recipient names, client names, personal contact info | Never | Scrubbed pre-sync |
| Habit patterns (role-keyed, person-keyed only by hash) | Yes | e.g. "AEs in MM pod: 73% check pipeline Mondays 8–9am" |
| Skill/prompt effectiveness metrics | Yes | "Prompt X triggered Y% approval rate" |
| Specialist agent call counts + error rates | Yes | Operational telemetry |
| User feedback ("that nudge was useful"/"that nudge was noise") | Yes | Keyed by user hash |

Scrubbing pipeline: regex for emails/phones/known client names + named-entity redaction + manual spot-check sampling before V1 ships. Reviewed quarterly.

## Personal wiki (V1 stub — dedicated session later)

The memory format per user. V1 ships a minimal working version; a dedicated session deepens it.

**Schema (V1):**
```
wikis/<slack_user_id>/
  identity.md       # role, team, manager, tenure, stated prefs
  patterns.md       # inferred habits: trigger, confidence, last_seen, source
  recent_focus.md   # rolling 14d: accounts, deals, people they're working
  relationships.md  # who they interact with + cadence (peer, manager, report, customer)
  conversations/    # per-thread memory summaries (not raw transcripts)
  consent.md        # what's authorized, when, revocation history
```

**Lifecycle:**
- Written by the conductor after each interaction + nightly rollup
- Queried by the conductor before responding (RAG-style)
- Patterns decay: confidence halves every 30 days without reinforcement
- `recent_focus.md` rotates weekly

**Deferred to dedicated session:** pattern-detection heuristics, wiki-size management, query interface, cross-wiki insight surfacing.

## Central self-improvement loop

Daily cron on the hub VM (e.g. `0 2 * * *`):

1. Pull last 24h of scrubbed telemetry from all team VMs.
2. Run an analyst prompt: "Given these signals, what's one skill to create or one existing skill to improve?"
3. Produce a proposed change (new prompt template, adjusted threshold, new habit rule).
4. Dry-run against yesterday's event log (replay).
5. If measurable improvement, open an approval gate to O (Slack DM).
6. On approval, push the updated skill/prompt to all team VMs on next sync.
7. Audit log on hub: what changed, why, which cohort triggered it.

**V1 self-improvement target (pick one):** prompt templates per role. Rest is V2.

**Why just prompts:** the specialists' decision thresholds are governance-gated and high-blast-radius. Letting the central hub tune prompts is low-risk and high-leverage. Threshold tuning and heuristic synthesis come after O has trusted the loop for a quarter.

## V1 scope cut

Ships in V1:
- Conductor = evolved OO; role-aware + personal-wiki-backed
- Per-team VM topology (start with 2 VMs: 1 sales pod + SLT, prove the pattern, then fan out)
- Consent onboarding + Gmail/Calendar/Fireflies token capture
- Full signal surface collection (rolling event log, 90d retention)
- Personal wiki V1 schema + nightly rollup
- Privacy scrubbing pipeline (regex + NER)
- Central hub: aggregation + prompt self-improvement cron (approval-gated)

Deferred to V2:
- Full fleet expansion past pilot
- Wiki pattern-detection deepening (dedicated session)
- Threshold tuning self-improvement
- Heuristic synthesis self-improvement
- Cross-team insight surfacing
- Mobile/web UI (Slack-only in V1)

## Prerequisites

**Must complete before Agent 7 build starts:**

1. **Phase 1 merge.** Six agents cleanly consolidated onto `main`, each passing in isolation and together. Non-negotiable — the conductor calls into them.
2. **Git remote configured** on the repo.
3. **HR/legal disclosure text** drafted with Loop People Ops.
4. **Pilot team selected** — one sales pod + SLT is the recommended start.
5. **Central hub VM provisioned** (can be Mac Mini initially, GCP later).

## Open questions for O

1. Exact team count — is my 10–14 VM estimate right, or is the org shape different? (Need the GTM org chart locked.)
2. Who approves self-improvement changes on the hub — just O, or O + one other person?
3. Does "Finance" mean AK only, or AK's team? Affects whether Finance is 1 VM or shared with SLT.
4. Pilot team recommendation: **Charles's ENT pod + SLT**. Sound right?
5. Retention policy: 90d raw is a starting point. Legal may want shorter (30d) or longer (1y). Needs a call with Legal.
6. Is there an existing Loop-internal privacy/AI policy that binds this build?

## Build order (after prerequisites)

1. Conductor core (OO evolution) — role-awareness + personal wiki V1 read/write
2. Consent onboarding flow
3. Signal collection per integration (one at a time: Slack → Gmail → Calendar → SF → Fireflies)
4. Privacy scrubbing pipeline
5. Central hub aggregation
6. Self-improvement cron (prompts only)
7. Pilot with 1 team + SLT — 30-day soak
8. Fleet expansion

Each stage passes a DoD gate before the next starts.
