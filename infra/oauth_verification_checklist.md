# Google OAuth App Verification — Submission Checklist

**Why this exists:** Google requires app verification for OAuth apps requesting "restricted" or "sensitive" scopes (Gmail and Calendar both qualify). Without verification, the app is capped at 100 users and shows an "unverified app" warning that is visually alarming and not appropriate for an internal corporate tool.

**Lead time:** Typically **2–6 weeks** end-to-end. Submit at Stage 1 of the Conductor plan; verification must be complete by Stage 8 (fleet expansion past the 6-user leadership pilot).

**Owner:** O. Loop's Workspace Admin (likely AK or equivalent) may need to co-sign for the brand verification step.

---

## Submission stage — what to gather BEFORE clicking "Submit for verification"

### 1. OAuth client setup
- [ ] GCP project: `loop-revops-conductor-hub` (created by `scripts/setup_gcp_hub.sh`)
- [ ] OAuth consent screen configured at: https://console.cloud.google.com/apis/credentials/consent?project=loop-revops-conductor-hub
- [ ] User type: **Internal** if Loop is on Google Workspace (skips most verification, valid only for @tryloop.ai users) — or **External** if not (full verification required).
  - **Decision needed:** confirm Loop is on Workspace and Internal is sufficient. If yes, this whole checklist becomes much shorter.
- [ ] Application type: Web application
- [ ] Authorized redirect URIs:
  - `http://127.0.0.1:8765/oauth2callback` (V1 — local Conductor flow on Tailscale-reachable laptop)
  - `https://conductor-hub-<hash>-uc.a.run.app/oauth2callback` (V1.5 — once Cloud Run hub serves the redirect)
- [ ] OAuth client ID + secret stored in Secret Manager as `conductor-hub-google-oauth-client-id` and `...-client-secret`

### 2. Brand information (shown to users on the consent screen)
- [ ] **App name:** "Loop Conductor" (decision needed — could also be "Loop RevOps Assistant")
- [ ] **App logo:** 120x120 PNG. Loop logomark works. **Need from Henry/marketing.**
- [ ] **App home page URL:** `https://tryloop.ai` (verified domain)
- [ ] **App privacy policy URL:** `https://tryloop.ai/privacy` (must mention employee data handling — **needs Legal review to confirm current privacy policy covers internal-tool data collection or whether a separate URL is needed**)
- [ ] **App terms of service URL:** `https://tryloop.ai/terms` (or "not applicable" — internal tool)
- [ ] **Authorized domains:** `tryloop.ai`, `run.app`

### 3. Scope justification (the hard part of External verification)

Google requires written justification for every restricted/sensitive scope. Below are draft justifications — refine before submission.

#### `https://www.googleapis.com/auth/gmail.readonly`

> Loop Conductor is an internal AI assistant for Loop AI's GTM team. It reads sent and received email metadata + bodies for the consenting employee in order to (1) detect overdue follow-ups on customer/prospect threads, (2) maintain the employee's personal context wiki for proactive nudging via Slack DM, and (3) provide meeting prep summaries by joining email context to upcoming calendar events.
>
> The data is read-only — Conductor never sends, deletes, or modifies email. Raw email content stays on a Loop-controlled server, retained for 90 days then deleted. No email content is shared with third parties or other Loop employees. Each user grants consent individually via an in-app flow (see demo video, time 0:34) and may revoke consent at any time, which deletes their stored email data within 24 hours.
>
> A narrower scope (e.g. `gmail.metadata`) is insufficient because the assistant must read message bodies to detect commitments made in email and detect content-driven action items — metadata alone (sender, recipient, subject, date) does not enable these features.

#### `https://www.googleapis.com/auth/calendar.readonly`

> Loop Conductor reads the consenting employee's calendar events (titles, times, attendees) to (1) provide meeting prep summaries on the day of, (2) detect customer-meeting cadence patterns for the personal context wiki, and (3) avoid sending nudges during the user's meeting blocks.
>
> The data is read-only — Conductor never creates, modifies, or deletes events. Same retention and revocation policy as Gmail above. A narrower scope is not available — `calendar.events.readonly` and `calendar.readonly` provide the same fields needed; we use the latter as the canonical name.

### 4. Demo video requirements (External verification only)

Google requires a screen-recorded demo (typically 1–3 minutes) showing the OAuth flow end-to-end. Must include:

- [ ] App home page (tryloop.ai) shown in browser
- [ ] App name visible during the consent screen
- [ ] Each restricted/sensitive scope explicitly clicked through on the consent screen
- [ ] One screen showing the data being used in-app (e.g. a Slack DM showing "Conductor noticed you haven't replied to <customer>'s email from 3 days ago")
- [ ] Privacy policy URL clearly visible at some point
- [ ] Upload to a Loop YouTube channel (unlisted) and submit URL

**Recording recommendation:** Loom or QuickTime → unlisted YouTube. Keep clean — Google reviewers reject videos with extraneous tabs visible, real customer data on screen, etc.

### 5. Privacy policy update (likely needs Legal)

The existing `tryloop.ai/privacy` policy is customer-facing. Google reviewers will check that the policy:
- [ ] Names "Loop Conductor" or generically describes the internal assistant
- [ ] States what Google data is collected (Gmail, Calendar) and why
- [ ] States retention period (90 days raw)
- [ ] States data is not shared with third parties
- [ ] Provides a contact for data deletion requests

**Decision needed:** does Legal want to amend the public privacy policy, or stand up a separate page like `tryloop.ai/conductor-privacy`? Google accepts either as long as the URL is reachable and contains the above elements.

---

## Internal-vs-External decision tree

```
Is Loop on Google Workspace? ── No ──> EXTERNAL submission, full checklist above (4–6 weeks)
        │
       Yes
        │
        ▼
Does Loop's Workspace admin allow Internal apps for this purpose?
        │
       Yes ──> INTERNAL submission. No verification required. App works only
                for @tryloop.ai users. Scopes still require admin consent in
                Workspace Admin Console (Apps → Web and mobile apps → Add app →
                OAuth app name → grant scopes for the org).
        │
       No  ──> EXTERNAL submission, full checklist (less common — most
                Workspace orgs allow Internal for internal tools).
```

**Recommendation:** start with Internal. The leadership pilot (6 users, all @tryloop.ai) ships faster and the External submission can run in parallel as a "blocking by Stage 8" gate.

---

## Submission steps (External path)

1. Configure OAuth consent screen (above).
2. Add scopes (above) — Google immediately flags them as requiring verification.
3. Click "Submit for verification".
4. Google emails follow-up questions within ~3–5 business days — respond same-day.
5. CASA (Cloud Application Security Assessment) may be triggered for sensitive scopes — adds 2–4 weeks. Loop's hub is single-tenant + internal so CASA letter requirements should be minimal but plan for it.
6. Verification confirmation email — keep for compliance file.

---

## After verification

- [ ] Update OAuth consent screen status to "In production"
- [ ] Remove the "100-user cap" warning from Conductor onboarding messaging
- [ ] Renew verification annually (Google sends reminder ~30 days before expiry)
- [ ] If scopes change in V2, re-verification is required for the added scopes only

---

## What this checklist is NOT

- NOT the actual submission — that happens in the GCP console.
- NOT a substitute for Legal review of the privacy policy text.
- NOT applicable to Salesforce, Slack, or Fireflies — those use existing service users / API keys, not Google OAuth.

## Owner / next action

O drives this. First action: **decide Internal vs. External** (5 min — open Workspace Admin Console, check user type setting). If Internal, this checklist collapses to ~3 steps (configure consent screen as Internal, add scopes, grant via Admin Console). If External, gather brand assets from marketing this week and book a 30-min slot with Legal to align the privacy policy.
