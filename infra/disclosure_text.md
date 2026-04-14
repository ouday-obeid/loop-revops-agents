# Conductor Disclosure & Consent — Draft for People Ops Review

**Status:** DRAFT — pending review by Loop People Ops + Legal before any user sees it.
**Purpose:** This is the text shown to a Loop employee the first time the Conductor (Agent 7) DMs them in Slack. It is the basis of their consent to be observed.
**Hash discipline:** When this file changes, its SHA256 changes; every `user_consent` row stores the hash of the disclosure the user actually saw. Old hashes are preserved indefinitely (legal artifact).

---

## What People Ops needs to decide

1. **Tone.** Conversational vs. legal. Draft below is conversational with a legal addendum. People Ops picks one or asks for both.
2. **Required vs. optional surfaces.** Draft treats every surface as opt-in. People Ops confirms.
3. **Retention numbers.** Draft says 90 days raw / indefinite scrubbed summaries. Legal needs to confirm.
4. **Revocation language.** Draft says revocation is immediate and deletes raw data for that surface. Legal confirms whether that satisfies any obligation we have.
5. **Workplace policy linkage.** Does this need to reference an existing AI/monitoring policy? If one is being drafted, this disclosure should land in the same review cycle.
6. **Manager visibility.** Draft is silent on whether managers can see their reports' Conductor data. Confirm policy: the system architecture says no (per-user wiki, no manager surface in V1) but we should say so explicitly.

---

## Draft 1 — Conversational (default for the consent DM)

Hi — I'm the Loop RevOps Conductor, an AI assistant built by O's team to help GTM folks at Loop work more effectively.

I work best when I know what's on your plate. To do that, I'd like to watch a few of your work surfaces — but only the ones you say yes to, and only after you complete a one-time setup.

**Here's exactly what I'd read, and why:**

| Surface | What I read | Why |
|---|---|---|
| Slack | DMs you send me, @-mentions of me, messages in channels I'm already in | To respond to you in context |
| Gmail | Subject lines and bodies of emails you send and receive | To know which deals/customers/people you're actively working |
| Google Calendar | Your meeting titles, times, and attendees | To know what you're heading into and coming out of |
| Salesforce | Opportunities and Accounts assigned to you | To see deal stage changes and activity |
| Fireflies | Transcripts of meetings you attended | To follow up on what was discussed |

**What I do with it:** I keep a personal "wiki" about your work — which deals you're focused on, who you talk to most, what patterns I notice in your week. I use that wiki to nudge you when something needs attention (a stale opportunity, an overdue follow-up, prep for tomorrow's meeting) and to delegate work to other agents on your behalf.

**What I never do:**
- I never share what I read about you with your manager, your peers, or anyone else at Loop without your explicit ask.
- I never send raw email/Slack/meeting content outside the team server you're on.
- I never watch you on a surface you didn't say yes to.

**Where the data lives:**
- Raw text (your emails, Slack, transcripts) stays on the team server I run on. It's deleted after 90 days.
- Anonymized patterns (no names, no client names, no contact info) flow to a central system that helps me get better at my job over time. O and Henry approve every change to my behavior; nothing is auto-pushed.

**You're in charge:**
- You can revoke any surface anytime by DMing me `/oo revoke <surface>` (e.g. `/oo revoke gmail`). When you do, the raw data for that surface is deleted within 24 hours.
- You can ask me to forget you entirely with `/oo nuke me`. That deletes your wiki, your tokens, and your event log. You'll still get the consent DM if you ever @-mention me again.

**Heads up:** This is a Loop-internal tool. Your activity inside it is visible to you and to me. If something I do feels wrong, tell O or Henry.

To set up, click the buttons below for the surfaces you want to grant. You can change your mind on any of them later.

---

## Draft 2 — Legal addendum (appended to Draft 1 or shown on click of "more detail")

By granting access to a surface listed above, you authorize Loop AI's RevOps Conductor system ("the system") to read the data described for that surface for the purpose of providing you with personalized work assistance. The system stores raw data on a Loop-controlled server for 90 days, after which it is deleted. The system stores anonymized, scrubbed summaries (with personal identifiers, client names, and contact information removed) indefinitely on a Loop-controlled central server for the purpose of system improvement. Changes to system behavior derived from these summaries are subject to two-person approval by Loop's Head of RevOps and Chief Revenue Officer before deployment. You may revoke access to any surface at any time, which will result in the deletion of raw data for that surface within 24 hours. Use of this system is voluntary; declining or revoking does not impact your employment, performance review, or standing at Loop AI. Questions about data handling should be directed to People Ops.

---

## Surface-by-surface OAuth scope detail (shown on consent button hover)

| Surface | Scope requested | What that allows |
|---|---|---|
| Gmail | `https://www.googleapis.com/auth/gmail.readonly` | Read messages and metadata. Does NOT allow sending, deleting, or modifying. |
| Calendar | `https://www.googleapis.com/auth/calendar.readonly` | Read events and free/busy. Does NOT allow creating, modifying, or deleting events. |
| Slack | (uses existing bot scopes — no new OAuth) | Bot can read channels it's in and DMs sent to it. Same as today. |
| Salesforce | (uses Loop's existing service user — no per-user OAuth) | Bot reads as service user, scoped to your owned Opps/Accounts. |
| Fireflies | (uses Loop's existing API key — no per-user OAuth) | Bot reads transcripts of meetings you attended. |

---

## Revocation flow (shown when user types `/oo revoke <surface>`)

1. Confirms the surface they're revoking.
2. Calls the provider's token revocation endpoint (Google for Gmail/Calendar; Slack for OAuth-granted scopes if any).
3. Deletes the row from `oauth_tokens`.
4. Writes an append-only revocation row to `user_consent` with timestamp and method (`decided_via=slack_command`).
5. Schedules a job to delete all `signal_events` for that user on that source within 24 hours.
6. DMs the user a confirmation including the wiki section affected (e.g. "I'll stop reading your Gmail. Patterns I learned from it will fade from my behavior over the next 30 days as they decay without reinforcement.").

---

## Open Qs for People Ops + Legal

- [ ] Does Loop have an existing employee monitoring or AI-tools policy this should reference?
- [ ] Is the 90-day raw retention defensible? Should it be 30 or shorter?
- [ ] "Indefinite scrubbed summaries" — do we need an upper bound? (Recommendation: keep indefinite for system-improvement value, but confirm.)
- [ ] Manager-visibility statement: confirm we want to commit in writing that managers cannot see direct-report Conductor data.
- [ ] Is the language "Use of this system is voluntary; declining does not impact your employment" sufficient, or does Legal want stronger non-coercion language?
- [ ] Does the revocation flow need to also offer "delete the scrubbed summaries derived from my data"? (Architecturally hard — they're aggregated. Need a stance.)
- [ ] Should this disclosure be re-presented annually on a renewal cadence, or is once-at-onboarding enough?

---

## Source of truth

- This document is the source of truth for what users are shown.
- The Conductor reads this file at boot, computes its SHA256, and writes the hash into every new `user_consent` row.
- A change to this file is a meaningful event — version it via git commit; do not edit silently.
