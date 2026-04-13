# SLT Metrics Agent Runbook

## Deploy

Agent 6 has no daemon of its own. It registers on the shared Slack dispatcher owned by OO. To bring it online:

1. Phase 0 infra deployed (`bash infra/bootstrap.sh`, `bash scripts/run_migrations.sh`).
2. `agents/oo/main.py::bootstrap()` calls `agents.slt_metrics.main.register_with_dispatcher()` alongside the other specialists.
3. Generate + install launchd plists from `shared/runtime/schedule.py` — the `slt-*` jobs ride the same generator as every other scheduled specialist.

### Required env

```bash
REVOPS_DB_URL                 # inherited from Phase 0
SLACK_BOT_TOKEN
SLACK_APP_TOKEN
SLACK_DEV_GUARD=0             # flip to 1 + SLACK_TEST_CHANNEL during dry-runs
ANTHROPIC_API_KEY             # narrator; narrate() returns the fallback string when absent
GDRIVE_FOLDER_ID              # optional — missing emits warning + local path
GDRIVE_SERVICE_ACCOUNT_JSON   # optional — JSON blob or filesystem path
BQ_CREDENTIALS_JSON           # optional — Loop Pulse unit economics, gap-flagged when absent
```

## Pause

```bash
launchctl bootout gui/$UID/com.loop-revops.slt-morning-snapshot
launchctl bootout gui/$UID/com.loop-revops.slt-daily-briefing
launchctl bootout gui/$UID/com.loop-revops.slt-friday-review
```

Pausing does not take down the OO daemon — `@oo slt <cmd>` still resolves; just returns stale data (no morning fetch, no draft DMs until the cron is reinstalled).

## Force a job now

```bash
cd $REVOPS_REPO_ROOT && source .venv/bin/activate
# Morning snapshot
python -c "from agents.slt_metrics.jobs import run_morning_snapshot; print(run_morning_snapshot())"
# Daily briefing (draft to O's DM)
python -c "from agents.slt_metrics.jobs import run_daily_briefing; print(run_daily_briefing())"
# Friday review
python -c "from agents.slt_metrics.jobs import run_friday_review; print(run_friday_review())"
```

`run_daily_briefing()` / `run_friday_review()` return `{"status": "sent", "gate_id": ..., "run_date": ..., "deals": N, "movers": M, ...}`. Status `"no_data"` means no snapshot rows for today or any earlier day — verify `pipeline_snapshots` has something (`SELECT MAX(snapshot_date) FROM pipeline_snapshots`).

## Regenerate a missed briefing

1. Force the morning snapshot: `run_morning_snapshot()` (safe to rerun — `ON CONFLICT DO NOTHING` on `(snapshot_date, opp_id)`).
2. Force the briefing with an explicit `today`:
   ```python
   from datetime import date
   from agents.slt_metrics.jobs import run_daily_briefing
   run_daily_briefing(today=date(2026, 4, 13))
   ```
3. Confirm the draft landed via `SELECT * FROM approval_gates WHERE action_type='slt_draft_review' ORDER BY id DESC LIMIT 1`.

## Logs

- Per-job stdout/stderr: `$REVOPS_REPO_ROOT/var/log/slt-<job>.out.log` / `.err.log`
- DB-backed: `agent_runs` filtered by `agent_name='slt_metrics'`, `audit_log` filtered by `agent_name='slt_metrics'`, `approval_gates` filtered by `action_type='slt_draft_review'`.

## Recalibrate forecast weights

Weights seed lives in `pipeline/config.py::WEIGHT_SEEDS`; the active version is stored on each run in `forecast_history.metadata.weights`.

1. `@oo slt weights propose` — Dirichlet-samples ~300 variants around the active seed, scores on Brier + MAPE against the last 2 quarters, surfaces top-3.
2. O reviews the proposal and picks one — or none. No auto-promotion.
3. `@oo slt weights set icp=0.22 stage=0.33 activity=0.15 timeline=0.15 call=0.15 version=v2-tuned-<date>` writes the new version; next scoring pass picks it up from `forecast_history.metadata`.
4. Old weights remain replayable — `forecast/backtest.py --weights-version <prior>` rebuilds the historical scores for comparison.

Target accuracy: ≥85% MAPE floor, 95% stretch (LUCID's prior claim, now backed by a real replay script).

## Handle a bad board metric

If Henry or Anand flags an ARR / NRR / coverage number:

1. Read `board_metrics/board_summary.build_board_metrics` to see which inputs composed the value.
2. For ARR: the SF closed-won rollup is authoritative (`board_metrics/arr_nrr.py::_sum_won_arr`). Verify the `Fixed_ARR__c` + `ACV__c` fields on the flagged opp in SF.
3. For NRR / logo / expansion: those come from Loop Pulse (BigQuery). If the unit-econ card is gap-flagged, the sheet renders `-- (Loop Pulse unavailable)` and the briefing omits them. Re-run after BQ credentials are active.
4. For coverage: `board_metrics/pipeline_coverage.build_coverage_report` divides open ACV by the segment quota. Verify `shared/db` `rep_config` (once populated) has the quota rows; absent quotas → coverage_ratio is None and the sheet reports `—`.

## Force a snapshot

Any date (not just today):

```python
from datetime import date
from agents.slt_metrics.pipeline.fetcher import fetch_open_opps
from agents.slt_metrics.pipeline.snapshotter import write_unscored_snapshot
write_unscored_snapshot(fetch_open_opps(), snapshot_date=date(2026, 4, 13))
```

Once scoring is wired end-to-end, swap to:

```python
from agents.slt_metrics.forecast.scorer import score_all
from agents.slt_metrics.pipeline.snapshotter import write_snapshot
from agents.slt_metrics.types import ForecastWeights
scored = score_all(fetch_open_opps(), ForecastWeights(), today=date.today())
write_snapshot(scored, snapshot_date=date.today())
```

## BigQuery re-auth

`agents/slt_metrics/bigquery/loop_pulse_client.py::LoopPulseClient` retries 3× with base=0.5s jitter, writes `integration_health` row per attempt, and raises `BigQueryUnavailable` after final failure. `is_connected()` short-circuits when `BQ_CREDENTIALS_JSON` is missing — the unit-economics sheet stays gap-flagged.

1. Rotate the service-account key in GCP.
2. Update `BQ_CREDENTIALS_JSON` in the secrets backend.
3. Smoke-test: `python -c "from agents.slt_metrics.bigquery.loop_pulse_client import LoopPulseClient; print(LoopPulseClient().is_connected())"`. Expect `True`.
4. Next scoring pass picks up the live values; the Unit Economics sheet swaps in without a code change.

## GDrive re-auth

1. O provisions a fresh service-account key with `drive.file` scope and write access to `/Loop AI/RevOps/Revenue Model/`.
2. Update `GDRIVE_FOLDER_ID` (the folder ID — not URL) and `GDRIVE_SERVICE_ACCOUNT_JSON` (JSON blob or path).
3. Smoke-test:
   ```python
   from pathlib import Path
   from agents.slt_metrics.gdrive.uploader import upload_workbook
   p = Path("/tmp/smoke.xlsx"); p.write_bytes(b"PK\x03\x04")
   print(upload_workbook(p))
   ```
   Expect `uploaded=True` with a `drive.google.com` link. Failure returns a `file://` fallback plus a warning string.

## Known risks

- **ICP proxy cap** — the 0.5 cap is a seed decision. If ToF populates `ICP_Score__c` for a subset of opps, scores diverge across the cohort. Spot-check the Deal Details sheet: `ICP Detail` column shows `sf-icp-score` vs `proxy-capped` so drift is visible at a glance.
- **Fireflies participant mapping** — call-intel relies on `OpportunityContactRoles.Contact.Email`. Sparse coverage drops the pillar to 0 for otherwise-live deals. Watch for `call_grade_avg = None` on AE cards; the fallback name-heuristic is in `forecast/call_intel.py`.
- **Draft-to-O-DM bottleneck** — O personally forwards every SLT deliverable. If O is on vacation, the daily briefing silently piles up in the DM. Audit `approval_gates` weekly for `slt_draft_review` rows older than 7 days still in `pending`.
- **Snapshot drift under cron miss** — `latest_snapshot_date(before=today+1)` means a missed morning cron still produces a briefing off yesterday's data. The `run_date` in the gate payload makes this explicit; don't treat the date silently.

## Phase Review before handoff

Run `docs/PHASE_REVIEW_PROMPT.md` against Monday board **18408463906** with updated column IDs for this agent's rows. The phase review covers: status stamp, session note with test count + coverage, actual done date, blast radius, phase column refresh. Do not hand off to O until that protocol is clean.

## Escalation contacts

- O — sole approver for `slt_draft_review`; manually forwards every SLT deliverable
- Henry (CRO) — primary forecast consumer; monthly deep-dive recipient
- Hutch (VP Sales) — AE + SDR scorecard consumer
- Anand (CEO) — narrative ARR/NRR recipient; does not query directly
