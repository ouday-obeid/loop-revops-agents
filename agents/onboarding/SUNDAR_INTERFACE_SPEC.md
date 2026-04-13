# Self-Serve Onboarding — Interface Spec Request

Hi Sundar — this is a short brief on the Onboarding agent that Ouday + Jackie
are standing up, and a request for the interface contract your self-serve
system will expose so the two can talk to each other.

**Short version:** the Onboarding agent handles the CSM-led "white glove"
onboarding flow for every Salesforce Closed Won deal. When your self-serve
product provisioning system goes live, we need a small handoff contract so the
agent knows (a) which customers skip the CSM flow entirely, and (b) how far
along self-serve provisioning is for the ones that don't.

Right now `agents/onboarding/self_serve_coordinator.py` is a stub — it returns
`{"status": "deferred", "reason": "awaiting spec from sundar"}` for any
onboarding. We'll wire it up the moment we have your spec.

---

## What the agent does today (for context)

- Polls SF every 5 minutes for new `Opportunity.StageName='Closed Won'` records.
- Creates an `Onboarding__c` record for each, populated with initial stages
  (`Overall_Onboarding_Status__c = 'Not Started'`, etc.).
- Monitors the onboarding through `JK_Onboarding_Stage__c` and
  `Overall_Onboarding_Status__c`.
- Alerts Jackie on stalls, unassigned owners, and handoff-checklist blockers.
- Weekly Friday digest to Jackie.

It assumes every Closed Won deal goes through a CSM. That assumption breaks
when your self-serve system is live — self-serve customers shouldn't trigger
a CSM DM or a handoff checklist.

---

## What we need from your interface

### 1. Eligibility signal — "is this deal self-serve?"

Some signal the agent can read from SF (or an API) at the moment a new Closed
Won opp is detected, so it can branch:

- **Self-serve eligible** → skip CSM assignment, skip handoff checklist,
  potentially skip `Onboarding__c` creation entirely (or create it with a
  different stage set).
- **Not self-serve** → current flow.

Options we can implement (pick whichever fits your system):

| Option | SF surface | Agent behavior |
|---|---|---|
| A. SF field on Opportunity | `Opportunity.Self_Serve_Eligible__c` boolean | Agent checks field at poll time. Requires Agent 5 to add the field. |
| B. SF field on Account | `Account.Self_Serve_Tier__c` picklist | Same, different object. |
| C. API callback | your service returns `{eligible: bool}` for an opp_id | Agent calls your endpoint before creating `Onboarding__c`. |
| D. Product SKU on Opp | line items contain a known self-serve SKU | No schema change, but brittle if the SKU evolves. |

**Preference:** A or B — lowest latency, simplest audit trail, fewest moving
parts. Tell us which field name to look for and we'll wire it in.

### 2. Provisioning state — "how far along is self-serve?"

For the customers who go through your system, the agent still wants to know:

- **Kickoff equivalent** — has the customer logged in / finished signup?
- **Activation equivalent** — is the first integration connected / first
  report generated?
- **Completed equivalent** — is provisioning done?

Either:
- **Option A:** your system writes back to `Onboarding__c` directly (new fields
  like `Self_Serve_Kickoff_Completed__c`, `Self_Serve_Activation_At__c`,
  `Self_Serve_Completed_At__c`), and the agent reads them.
- **Option B:** you expose a REST endpoint the agent polls
  (`GET /self-serve/status/{account_id}`) that returns a simple JSON:
  ```json
  {
    "account_id": "001XXX",
    "signup_at": "2026-04-13T10:00:00Z",
    "first_integration_at": "2026-04-13T11:30:00Z",
    "activation_at": null,
    "completed_at": null,
    "blocked_reason": null
  }
  ```

**Preference:** A, because SF is the system of record and Agent 4 (CS) will
already be reading health signals from there. If your platform can't write to
SF, B works — we'll handle the polling cadence.

### 3. Failure / blocked signal

If a self-serve customer gets stuck — OAuth failed, account creation errored,
trial converted but payment failed — the agent should be able to flag this so
a CSM can reach in. What signal will you emit for this?

Minimum viable: a single field on `Onboarding__c` that the self-serve system
sets to a non-null value when the customer is blocked, e.g.
`Self_Serve_Blocked_Reason__c` ("oauth_failed" / "payment_failed" / "unknown").

---

## What we will build on our side once we have your spec

1. Replace the stub in `self_serve_coordinator.py` with:
   - `coordinate(onboarding_id)` → decides self-serve vs CSM flow.
   - `poll_status(onboarding_id)` → reads your state (if API) or SF fields
     (if written back), updates `Onboarding__c.Overall_Onboarding_Status__c`
     accordingly.
   - `handle_blocked(onboarding_id, reason)` → posts a DM to Jackie with the
     account + reason + link to your admin UI (if you have one).
2. Branch the closed-won poller at the top: if self-serve eligible → minimal
   `Onboarding__c` + skip CSM enforcement. If not → current flow.
3. Skip handoff checklist items that aren't relevant to self-serve (kickoff,
   implementation plan, etc.). The checklist is already tri-valued so this is
   a one-tuple edit.

---

## Questions for you

- **Timing:** when do you expect the self-serve system's v1 to be live? We're
  rolling out the CSM agent now and would rather not build two versions of the
  coordinator, so ideally we build once against your spec.
- **Scope:** what does "self-serve complete" mean on your side? (Pilot signed?
  First active location? Subscription active?)
- **SF writes:** is your system going to write to SF directly, or do you want
  us to pull from your API?
- **Account model:** will self-serve ever upsell into CSM-led? If yes, the
  agent needs to handle the transition (right now it assumes one-and-done).
- **Opp attribution:** are self-serve deals going through the same SF `Opportunity`
  pipeline as sales-led, or a separate object / flow?

Send any spec / schema / endpoint docs to Ouday. We'll turn them into a
replacement for the stub within the same session — it's a single file, isolated
from the rest of the agent.

— Ouday
