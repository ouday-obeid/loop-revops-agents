# Onboarding Agent — Walkthrough for Jackie

Hi Jackie — this is a short tour of the new Onboarding agent that's replacing
the broken SF Flow (`MT (Oppty) Closed Won Generates Onboarding`). Everything
below is already running in the `revagents` sandbox behind a dev-guard that
routes test messages to `#revops-agents-test` — nothing hits `#cs-team` or
customers until you say so.

**Your role:** department-head approver. O has override authority, but you're
the primary yes/no on everything that touches a CS record.

---

## What the agent does for you

| Pain today | What the agent does |
|---|---|
| Closed Won deals land without an `Onboarding__c` record (26+ incidents) | Every new Closed Won opp spawns a correctly-populated `Onboarding__c` within 5 min. Idempotent — the same opp will never spawn two records. |
| CSMs lose track of which onboardings are stalled | Every 6 hours, the agent scans `JK_Onboarding_Stage__c` + `Overall_Onboarding_Status__c`. If neither field advances in ≥5 business days, you get a DM with an "Extend 3 days" or "Escalate" button. |
| Unassigned onboardings fall through | If an Onboarding lands with no `OwnerId`, you get a DM with a "Reassign CSM" button. |
| Sales → Impl → CS handoffs miss items | A 6-point checklist runs on demand (`@oo onboarding handoff <account>`). Items that can't be checked yet degrade gracefully — they don't hard-block. |
| Friday status-check meetings eat time | A digest lands in your channel every Friday 9 AM ET with last-7-days counts (created / stalled / reassignment requests). |

---

## Slack commands

Type these as a DM to the bot or with `@oo` in any channel it's in:

```
@oo onboarding ping                        — health check (should get "pong")
@oo onboarding help                        — full command list
@oo onboarding status <account>            — snapshot of that account's Onboarding__c
@oo onboarding stalls                      — everything stalled ≥5 business days
@oo onboarding stalls 7                    — raise the threshold when holidays push things
@oo onboarding unassigned                  — all Onboardings with null OwnerId
@oo onboarding stuck-locations [account]   — stuck locations (schema-gapped right now)
@oo onboarding handoff <account>           — run the 6-item handoff checklist
@oo onboarding skip <opp_id> <reason>      — override a blocking handoff item
@oo onboarding assign <gate_id> <user_id>  — complete a CSM reassignment
@oo onboarding backfill --preview          — count historical CW gaps (read-only)
```

Account names support partial match (`TestCo 05` matches `[Onb Walkthrough] TestCo 05`).

---

## Approval DMs you'll see

Three kinds of buttons will appear in your DMs. Click Approve or Reject.

### 1. `csm_reassignment`
> *Onboarding a01XXX for Acme has no CSM. Who should own it?*
> [ Approve ] [ Reject ]

**What happens on Approve:** the gate is marked approved in the audit log. To
actually reassign, reply in-thread with `@oo onboarding assign <gate_id> <user_id>`
where `<user_id>` is the SF user you want to own the record. The agent will
`UPDATE Onboarding__c SET OwnerId = <user_id>` and write an audit row.

### 2. `onboarding_complete`
> *Onboarding a01XXX for Acme is being marked Completed — confirm?*
> [ Approve ] [ Reject ]

**Use this** when you manually flip `Overall_Onboarding_Status__c = 'Completed'`
on a record and the agent catches it — this is the last human checkpoint before
the record flows into Agent 4 (CS) for health scoring.

### 3. `skip_milestone`
> *Handoff checklist for Acme has 2 failing items. Proposed override:
> "products_priced" (justification: 'trial contract, no line items by design')*
> [ Approve ] [ Reject ]

**Requires a justification** (the agent will not let you approve without one).
This exists so you can push a handoff through when a blocker is legitimately
not applicable — think free trials, pilot-non-conversion wind-downs, etc.

---

## The 6 handoff checklist items (seed set — you refine these)

The checklist is a single Python tuple in `handoff_checklist.py`. Editing it
is a one-file diff — no schema change, no downtime. Current seed items:

1. **`products_priced`** — every `OpportunityLineItem` has `UnitPrice` > 0.
   Directly mitigates problem C6 (products on Onboarding don't match Opp).
2. **`stakeholders_captured`** — ≥1 `OpportunityContactRole` with `IsPrimary=true`.
3. **`contract_countersigned`** — `Opportunity.DocuSign_Status__c = 'Completed'`.
   Degrades to informational if the DocuSign field is missing (PandaDoc migration).
4. **`zenskar_billing`** — informational until Zenskar integration ships;
   activated via env flag `ONBOARDING_ZENSKAR_GATE_ACTIVE=1`.
5. **`kickoff_on_calendar`** — `Onboarding__c.Kickoff_Status__c IN
   ('Kickoff Scheduled', 'Kickoff Held')`.
6. **`implementation_plan_attached`** — `ContentDocumentLink` with
   "Implementation" in the title, linked to the opp or onboarding.

Each check returns one of three values:
- ✅ **Pass** — the condition is met.
- ❌ **Fail** — condition unmet and should block handoff unless overridden.
- ➖ **Informational** — can't be evaluated yet (field missing, feature not live).
  Does NOT block.

**Questions for you during the walkthrough:**
- Are all 6 items the right items?
- Should any ❌ items actually be ➖ (too strict), or vice versa?
- Are there additional items we should add (integration readiness, payment setup)?
- For `zenskar_billing`, what SF field will indicate "Zenskar provisioned"
  once that integration ships?

---

## What the agent won't do

- It will not create or modify SF schema. If a field is missing, it logs a task
  for Agent 5 (RevOps Support) and waits.
- It will not send customer-facing emails — those stay with CSMs.
- It will not touch churn risk / health scores / renewals — those are Agent 4.
- It will not act on `Opportunity.Onboarding_Record_Created__c` until Agent 5
  adds that field (right now it uses a fallback dedup strategy that's slightly
  more SOQL-heavy but safe).

---

## Sandbox walkthrough — what we'll run together (Week 10)

We'll go through these in order. Everything will post to `#revops-agents-test`
because dev-guard is on.

1. **`@oo onboarding ping`** — confirm the bot responds.
2. **`@oo onboarding backfill --preview`** — see the historical-gap count.
3. **Seed a fresh Closed Won opp in sandbox** — watch the agent create the
   `Onboarding__c` within the next 5-min poll tick.
4. **`@oo onboarding status <that account>`** — confirm the record is visible.
5. **Pre-seeded stalled record** (6 business days on `DS_Overall_Onboarding_Status_In_Progress__c`):
   run `@oo onboarding stalls` and see the DM with the Extend/Escalate buttons.
6. **Click "Extend 3 days"** — the alert silences for 72h + 3 business days;
   the audit log records who clicked.
7. **Pre-seeded unassigned record** — see the CSM-reassignment DM. Click Approve,
   then reply `@oo onboarding assign <gate_id> 005XXXXX` with a real SF user id.
   Watch the `OwnerId` update in SF.
8. **`@oo onboarding handoff TestCo 03`** — see the 6 checks render. One item
   will pass; the rest fail (sandbox opp has no line items or contacts).
9. **`@oo onboarding skip 006XXX "trial contract, no line items by design"`** —
   create a skip_milestone gate. Approve it from your DM.
10. **Friday digest preview** — we'll manually trigger `send_jackie_weekly_digest`
    and confirm the numbers match what you'd expect for the sandbox.

---

## Go-live day

Three env flags, no code change:
1. `SF_ORG_ALIAS=revagents` → production alias.
2. Unset `SLACK_DEV_GUARD` (or set to `0`) — messages start flowing to real channels.
3. Set `ONBOARDING_DIGEST_CHANNEL` to `#cs-team` (or wherever you want the
   Friday digest to land).

Rollback is a one-liner: set `SF_ORG_ALIAS` back to sandbox and the agent stops
touching prod immediately.

---

## Questions to think about before we walk through

- Which SF user should be the default owner of unassigned onboardings while you
  decide who to reassign to? (Today: null, which triggers the DM.)
- Do you want the Friday digest to include a list of stalled onboardings by
  name, or just counts?
- What threshold (default 5 business days) do you want for stall alerts?
- Who besides you should get copied on `csm_reassignment` DMs? (Today: just
  you + O.)
- Any CSMs you want mapped Slack-side for direct stall DMs? We build a
  `OwnerId → Slack user id` map in `ONBOARDING_CSM_SLACK_MAP`.

— Ouday
