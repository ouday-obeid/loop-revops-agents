# Bucket C — Human-in-the-loop Slack drafts (2026-04-17)

Draft only. **Do NOT send without O approval.** Each message includes
context, the specific verification ask, and the exact command the
stakeholder should run.

Baseline SF org is live (RevAgents sandbox). Once these 5 Bucket C asks
complete, the corresponding Monday subitems flip green and Scenario 1
can fire.

---

## 1. Hutch (VP Sales) — `@hutch.fisher`

**Covers Monday subitems:**
- `11736877678` Hutch dept-head access (Sales Reps)
- `11736873568` Hutch dept-head access (ToF)
- `11736901383` Hutch dept-head access (SLT)
- `11736892543` Leaderboards 4wk vs Hutch's tracking
- `11736901134` AE scorecards spot-check last quarter
- `11736896152` Scorecards output review (already stamped, but leaving draft in case)

**Draft:**
```
Hey Hutch — dept-head access verification for the three agents you touch. Two quick DM tests, ~2min total:

1. Sales Reps — `@oo sales-reps leaderboard ae 2026-W15`
   Should return AE leaderboard for last week. Reply "ok" here if you get output.

2. ToF — `@oo tof score acme-franchise-group.com`
   Should return ICP grade + pillar breakdown for that domain.

3. SLT — `@oo slt pipeline`
   Should return current open-pipeline rollup. If you want the Nate MM rollup specifically: `@oo slt scorecard nate-team`.

Once you confirm all three, I'll close out the rest of your bucket (AE scorecards spot-check, leaderboard 4wk comparison). Standing by.
```

---

## 2. Henry (CRO) — `@henry`

**Covers Monday subitems:**
- `11736936215` Henry dept-head access (SLT)
- `11736886861` Board ARR/NRR vs finance internal (±1%)

**Draft:**
```
Hey Henry — two SLT agent verifications when you have 90 seconds:

1. Dept-head access — DM @oo with: `@oo slt pipeline`
   Should return current open-pipeline rollup. Reply here with what you see.

2. Board metrics sanity — once (1) works, try: `@oo slt board-metrics 2026-03`
   I want you to compare ARR / NRR / gross retention to finance's internal numbers for March. We're targeting ±1% parity. If it's off, reply with the delta and I'll recalibrate.
```

---

## 3. Jackie (Head of CS) — `@jackie`

**Covers Monday subitems:**
- `11736911893` Jackie dept-head access (Onboarding)
- `11736886663` Jackie dept-head access (CS)
- `11736866919` Churn risk distribution review
- `11736885165` Renewal brief review for 1 upcoming call

**Draft:**
```
Hey Jackie — when you've got 5 min, four CS/Onboarding agent verifications. All DMs to @oo — nothing breaks anything:

1. Onboarding access — `@oo onboarding status <customer_name>`
2. CS access — `@oo cs status <customer_name>`
3. Churn sanity — `@oo cs churn-risk distribution`
   Gives you the distribution across your book. Look at the top-20 highest-risk. Anyone obviously NOT at risk? That's a false positive I need to tune out.
4. Renewal brief — `@oo cs renewal-brief <upcoming_renewal_account>`
   Gives you the brief for one upcoming renewal call. I want your "would I use this in the call? y/n + what's missing" take.

Reply here with each result. Takes precedence over my own calendar — just ping when done.
```

---

## 4. Brian (Head of SDR) — `@brian.sufalko`

**Covers Monday subitems:**
- `11736885207` SDR scorecards last week spot-check

**Draft:**
```
Brian — SDR scorecard verification, 2 min:

Run: `@oo slt scorecard sdr-team week 2026-W15`

That returns the SDR scorecard for last completed week. Spot-check two SDRs of your choosing against what you track manually. Any delta over 10% on activity counts, meetings set, or qualified-out rate — reply here with the names and deltas.

If parity is tight, I'll lock the scoring model at the current weights.
```

---

## 5. Sundar (Loop AI eng) — `@sundar`

**Covers Monday subitems:**
- `11736911893` partial — Sundar self-serve interface spec reply (onboarding)

**Draft:**
```
Sundar — following up on the onboarding self-serve interface spec I sent last week. The doc is at ~/loop-revops-agents/agents/onboarding/SUNDAR_INTERFACE_SPEC.md.

Two open questions:
1. Preferred contract for "self-serve customer completed setup" → should this post to a webhook, write to an Opportunity, or drop an event into Pub/Sub?
2. Naming for the status field — we're using `Self_Serve_State__c` with picklist {awaiting_first_login, active, stalled, completed}. Does that align with product's internal vocabulary?

Reply when you get a chance. Not urgent but blocks the Week-10 cutover for the onboarding agent.
```

---

## Sending sequence (when O approves)

1. **Hutch** (most load-bearing — his unlocks Scenario 1 fire)
2. **Henry** (SLT sign-off)
3. **Jackie** (CS + Onboarding; 2 parents covered)
4. **Brian** (single SDR check)
5. **Sundar** (lowest urgency; Week-10 blocker)

Stamp each subitem's Notes with "ask sent YYYY-MM-DD — awaiting <stakeholder>" when O approves each send.
