# GDrive + BigQuery Setup — Agent 6 (SLT Revenue Metrics)

Safe, step-by-step provisioning for the two deferred integrations. Both paths are gap-flag-safe — if you pause partway through, Agent 6 keeps running. The Unit Economics sheet stays flagged; the workbook stays local with a `file://` link. Nothing is load-bearing on either until the env vars land.

- **GDrive** — ship first. Lowest risk. Failure mode: local path in the briefing instead of a Drive link.
- **BigQuery** — delicate. LUCID shares the dataset. Follow the LUCID safety rails in Part 2 exactly.

---

## Pre-flight — applies to both

Run these once before either section.

1. **Pick a safe home for credential JSONs.** Do not put them in the repo.
   ```bash
   mkdir -p ~/.secrets/loop-revops
   chmod 700 ~/.secrets/loop-revops
   ```
2. **Confirm `.gitignore` covers secrets.**
   ```bash
   cd /Users/ottimate/loop-revops-agents
   grep -E '^\.env$|^\*\.json$' .gitignore || echo "NEEDS GITIGNORE ENTRY"
   ```
   If that prints `NEEDS GITIGNORE ENTRY`, add `.env` and `*.service-account.json` to `.gitignore` and commit before going further.
3. **Locate the repo's `.env`** — both env vars land here:
   ```bash
   ls -la /Users/ottimate/loop-revops-agents/.env
   ```
   If missing, copy from the template:
   ```bash
   cp /Users/ottimate/loop-revops-agents/.env.example /Users/ottimate/loop-revops-agents/.env
   ```
4. **Snapshot current state** so you can diff / revert cleanly:
   ```bash
   cp /Users/ottimate/loop-revops-agents/.env /Users/ottimate/loop-revops-agents/.env.pre-gdrive-bq
   ```

Both sections are additive — nothing existing in `.env` gets overwritten.

---

## Part 1 — GDrive (ship first)

**What breaks if you skip:** nothing. The uploader returns a `file://` local path and a warning in O's DM. The briefing still sends.

**What can go wrong if rushed:** service-account key shared too broadly, or the folder shared with the wrong account. Both are recoverable by revoking the key.

### Step 1.1 — Pick the GCP project

**WARNING:** Do not use LUCID's project for this. LUCID lives in `arboreal-vision-339901`. Use a separate project for Agent 6's Drive access so rotations don't cross-contaminate.

Recommended: create (or reuse) a project named `loop-revops-agents`.

```bash
gcloud projects list --filter="name:loop-revops*"
```

If no match, create it:
```bash
gcloud projects create loop-revops-agents --name="Loop RevOps Agents"
gcloud config set project loop-revops-agents
```

If one already exists, `gcloud config set project <project-id>` and skip the create.

### Step 1.2 — Enable the Drive API

```bash
gcloud services enable drive.googleapis.com --project=loop-revops-agents
```

Idempotent — safe to re-run.

### Step 1.3 — Create the service account

```bash
gcloud iam service-accounts create loop-revops-agent6-gdrive \
  --display-name="Loop RevOps Agent 6 — GDrive Uploader" \
  --project=loop-revops-agents
```

Full email: `loop-revops-agent6-gdrive@loop-revops-agents.iam.gserviceaccount.com`. Copy it — you need it in Step 1.6.

**WARNING:** Do not grant any project-level IAM roles. Drive access is granted per-folder via sharing, not via IAM. Project-level Drive roles are overly broad and outlive folder deletions.

### Step 1.4 — Generate and store the key

```bash
gcloud iam service-accounts keys create \
  ~/.secrets/loop-revops/gdrive-sa.json \
  --iam-account=loop-revops-agent6-gdrive@loop-revops-agents.iam.gserviceaccount.com
chmod 600 ~/.secrets/loop-revops/gdrive-sa.json
```

**WARNING:** Do not paste this JSON into Slack, email, or a ticket. If it leaks, revoke immediately:
```bash
gcloud iam service-accounts keys list --iam-account=loop-revops-agent6-gdrive@loop-revops-agents.iam.gserviceaccount.com
gcloud iam service-accounts keys delete <KEY_ID> --iam-account=loop-revops-agent6-gdrive@loop-revops-agents.iam.gserviceaccount.com
```

### Step 1.5 — Create the Drive folder

In the Drive web UI, logged in as O's account:
1. Navigate to `Loop AI / RevOps /` (create parents if missing).
2. Create `Revenue Model`, then a child folder matching the current year, e.g., `2026`.
3. Open the `2026` folder. The URL will look like `https://drive.google.com/drive/folders/1A2B3C...xYz`. The suffix after `/folders/` is the `FOLDER_ID`. Copy the ID only — not the URL.

### Step 1.6 — Share the folder with the service account

Still in Drive UI, on the `2026` folder:
1. Right-click → Share.
2. Paste `loop-revops-agent6-gdrive@loop-revops-agents.iam.gserviceaccount.com`.
3. Role: **Editor**. Turn off "Notify people" (service accounts can't receive email).
4. Send.

**WARNING:** Share only the `2026` folder, not the whole `Loop AI` tree. The scope is intentionally tight — the SA should not see anything above `Revenue Model/2026/`.

### Step 1.7 — Add the env vars

Append to `/Users/ottimate/loop-revops-agents/.env`:
```
GDRIVE_FOLDER_ID=<paste the folder ID from step 1.5>
GDRIVE_SERVICE_ACCOUNT_JSON=/Users/ottimate/.secrets/loop-revops/gdrive-sa.json
```

Both values go in as plain text. The loader accepts either a filesystem path or an inline JSON blob — the path form is cleaner and keeps secrets out of any process env dump.

### Step 1.8 — Install the Drive client libraries

```bash
cd /Users/ottimate/loop-revops-agents && source .venv/bin/activate
pip install google-api-python-client google-auth
```

These are declared in `pyproject.toml` but load lazily — confirm they are installed in the active venv before the smoke test.

### Step 1.9 — Smoke test

```bash
cd /Users/ottimate/loop-revops-agents && source .venv/bin/activate
python - <<'PY'
from pathlib import Path
from agents.slt_metrics.gdrive.uploader import upload_workbook
p = Path("/tmp/agent6-gdrive-smoke.xlsx")
p.write_bytes(b"PK\x03\x04 smoke")
result = upload_workbook(p)
print(result)
PY
```

Expected:
- `uploaded=True`
- `link` starts with `https://drive.google.com/`
- `warning` is `None`

If `uploaded=False`: read `result.warning`. Two common failures:
- "folder not shared with SA email" — Step 1.6 wrong.
- "credentials invalid" — Step 1.4 wrong.

Neither affects briefings; the uploader falls back to a `file://` link and logs.

Delete the smoke upload from Drive when satisfied.

---

## Part 2 — BigQuery (the delicate one)

**WARNING:** Read this entire section before running any command. LUCID uses BigQuery in project `arboreal-vision-339901`, dataset `account_health`, tables `signal_log`, `accounts_master`, `stage4_usage`. **None of that changes.** Agent 6 will get its own service account with read-only scope against the same dataset. LUCID's key stays untouched.

**What breaks if you skip:** nothing. The Unit Economics sheet renders `-- (Loop Pulse unavailable)` and the scorer / briefing both continue normally.

**What can go wrong if rushed:**
- Accidentally rotating LUCID's key → LUCID 500s within minutes.
- Granting `BigQuery Admin` instead of `Data Viewer` + `Job User` → the SA could drop tables.
- Pointing at the wrong dataset → silent NULLs in the workbook.

### Step 2.1 — Do NOT touch LUCID's existing service account

Read-only check so you can see what LUCID uses, and confirm you are NOT going to touch it:
```bash
gcloud iam service-accounts list --project=arboreal-vision-339901
```

LUCID's SA will typically be named `lucid-*` or `account-health-*`. **Memorize its name. You will not issue a single `gcloud` command against it today.** Any `gcloud iam service-accounts keys delete` against that account will page LUCID oncall.

### Step 2.2 — Create Agent 6's BigQuery service account in LUCID's project

Agent 6's SA must live in `arboreal-vision-339901` because that's where the dataset is. It is a new, separate identity.

```bash
gcloud iam service-accounts create loop-revops-agent6-bigquery \
  --display-name="Loop RevOps Agent 6 — Loop Pulse Reader" \
  --project=arboreal-vision-339901
```

Full email: `loop-revops-agent6-bigquery@arboreal-vision-339901.iam.gserviceaccount.com`.

### Step 2.3 — Grant the minimum two roles, dataset-scoped

**WARNING:** Never grant `roles/bigquery.admin`. Never grant `roles/bigquery.dataEditor`. Agent 6 only reads.

Two roles are needed:
- `roles/bigquery.jobUser` — permits running queries (only available at project level; cannot modify data).
- `roles/bigquery.dataViewer` — permits reading the dataset. Scope this to the dataset, not the project.

Grant `jobUser` at project level:
```bash
gcloud projects add-iam-policy-binding arboreal-vision-339901 \
  --member="serviceAccount:loop-revops-agent6-bigquery@arboreal-vision-339901.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser" \
  --condition=None
```

Grant `dataViewer` at **dataset level only**. This uses `bq`, not `gcloud`:
```bash
bq show --format=prettyjson arboreal-vision-339901:account_health > /tmp/dataset-acl.json
```

Open `/tmp/dataset-acl.json`, find the `"access"` array, and **add** (do not replace) this block:
```json
{
  "role": "READER",
  "userByEmail": "loop-revops-agent6-bigquery@arboreal-vision-339901.iam.gserviceaccount.com"
}
```

Apply:
```bash
bq update --source=/tmp/dataset-acl.json arboreal-vision-339901:account_health
rm /tmp/dataset-acl.json
```

**WARNING:** Before running `bq update`, diff the file against the `bq show` output. You want exactly one new entry added to `access`. If any existing entry is missing, you have just revoked LUCID's access. Do not proceed until the diff shows only an addition.

Verify:
```bash
bq show --format=prettyjson arboreal-vision-339901:account_health | grep -A1 userByEmail
```

The Agent 6 SA email must appear in the output.

### Step 2.4 — Generate and store the key

```bash
gcloud iam service-accounts keys create \
  ~/.secrets/loop-revops/bigquery-sa.json \
  --iam-account=loop-revops-agent6-bigquery@arboreal-vision-339901.iam.gserviceaccount.com
chmod 600 ~/.secrets/loop-revops/bigquery-sa.json
```

### Step 2.5 — Add the env vars

Append to `/Users/ottimate/loop-revops-agents/.env`:
```
BQ_CREDENTIALS_JSON=/Users/ottimate/.secrets/loop-revops/bigquery-sa.json
BQ_PROJECT=arboreal-vision-339901
```

`BQ_PROJECT` is optional — the client falls back to the `project_id` inside the JSON — but explicit is safer.

### Step 2.6 — Install the BigQuery client library

```bash
cd /Users/ottimate/loop-revops-agents && source .venv/bin/activate
pip install google-cloud-bigquery
```

### Step 2.7 — Isolated smoke test (read-only)

```bash
cd /Users/ottimate/loop-revops-agents && source .venv/bin/activate
python - <<'PY'
from agents.slt_metrics.bigquery.loop_pulse_client import LoopPulseClient
c = LoopPulseClient()
print("connected:", c.is_connected())
PY
```

Expected: `connected: True`.

If `False`, read `integration_health` for the exact failure:
```bash
sqlite3 "$(grep REVOPS_DB_URL /Users/ottimate/loop-revops-agents/.env | cut -d= -f2 | tr -d '"' | sed 's|sqlite:///||')" \
  "SELECT * FROM integration_health WHERE integration='slt_loop_pulse' ORDER BY id DESC LIMIT 3"
```

Common first-run failures:
- `AccessDenied` on `bigquery.jobs.create` → Step 2.3 `jobUser` binding didn't apply. Re-run.
- `AccessDenied` on dataset → Step 2.3 ACL edit missed. Re-check with `bq show`.
- `DefaultCredentialsError` → Step 2.4 path wrong, or file permissions blocked reads.

### Step 2.8 — Read-only query test against a known LUCID table

```bash
python - <<'PY'
from agents.slt_metrics.bigquery.loop_pulse_client import LoopPulseClient
c = LoopPulseClient()
rows = c.query("SELECT COUNT(*) AS n FROM `arboreal-vision-339901.account_health.accounts_master`")
print(rows)
PY
```

Expected: one row with an integer `n`. If this succeeds, LUCID's dataset is readable; Agent 6's Unit Economics sheet will light up on the next briefing run with no code change.

**WARNING:** Do NOT run DDL or DML here. No `CREATE`, no `INSERT`, no `UPDATE`, no `MERGE`. The `dataViewer` role blocks them, but don't rely on that — don't type them.

### Step 2.9 — Confirm LUCID is still healthy

After all of the above:
```bash
curl -sI https://<lucid-prod-url>/api/health
```

Or trigger a known LUCID read path and confirm data comes back. This is the final "nothing broken" check.

---

## Rollback & rotation

**GDrive rollback** — comment out `GDRIVE_FOLDER_ID` in `.env`, restart the cron. Uploader returns local paths; briefings keep shipping.

**BigQuery rollback** — comment out `BQ_CREDENTIALS_JSON`, restart. Unit Economics returns to gap-flagged.

**Rotation (90-day SA key hygiene)** — create new key, swap env var, smoke-test, THEN revoke old key. Never revoke first:
```bash
# 1. New key
gcloud iam service-accounts keys create ~/.secrets/loop-revops/<svc>-sa.NEW.json --iam-account=...
# 2. Swap the path in .env. Restart.
# 3. Smoke test.
# 4. ONLY after success, delete old:
gcloud iam service-accounts keys delete <OLD_KEY_ID> --iam-account=...
```

---

## Final verification after both parts

```bash
cd /Users/ottimate/loop-revops-agents && source .venv/bin/activate
python -c "from agents.slt_metrics.jobs import run_daily_briefing; print(run_daily_briefing())"
```

Expected: return includes `"status": "sent"` plus a `gate_id`. Check O's DM for the draft — the Unit Economics section should show real numbers (not `-- (Loop Pulse unavailable)`) and the workbook link should resolve to Drive.

The two sections are independent — ship GDrive today, BigQuery whenever the LUCID IAM dance can be done calmly. Neither is urgent; the agent is already shipping briefings without them.
