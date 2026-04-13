---
name: slt_metrics
description: SLT Revenue Metrics specialist — absorbs the manual Outbounder revenue workbook and the LUCID 4-pillar forecast scorer into one automated pipeline. Ships a daily 8:30 ET briefing, a Friday 15:30 weekly review, an on-demand 9-sheet Excel workbook, and @oo slt query surface to Henry (CRO), Hutch (VP Sales), Anand (CEO). Draft-to-O-DM-only routing — O forwards every SLT deliverable manually.
---

# SLT Metrics Agent (Agent 6)

Primary consumers in primacy order: **Henry** (CRO, weekly forecast + monthly deep dive), **Hutch** (VP Sales, AE + SDR scorecards), **Anand** (CEO, narrative ARR/NRR), **O** (gatekeeper — reviews every SLT-facing deliverable; no auto-channel fanout).

## Commands

- `@oo slt ping` — health check
- `@oo slt forecast <quarter>` — 5-pillar commit/best/weighted rollup + workbook link
- `@oo slt movers <period>` — top 10 movers by |ΔACV| across the window
- `@oo slt scorecard <scope>` — AE / SDR / team cards (Hutch access)
- `@oo slt briefing` — on-demand daily draft to O's DM
- `@oo slt friday` — on-demand weekly review draft to O's DM
- `@oo slt weights show|set|propose` — show the active ForecastWeights, set a pillar, propose Dirichlet-tuned candidates
- `@oo slt backtest <from> <to>` — MAPE + Brier against OpportunityHistory replay
- `@oo slt help` — static usage

## Scheduled jobs

- M-F 06:30 ET — `slt-morning-snapshot` — fetch + snapshot open pipeline to `pipeline_snapshots`
- M-F 08:00 ET — `slt-daily-briefing` — compose daily → `slt_draft_review` gate → O's DM
- Fri 15:30 ET — `slt-friday-review` — compose weekly → `slt_draft_review` gate → O's DM

## Approval gates

| action_type | tier | approver | effect on approve |
|---|---|---|---|
| `slt_draft_review` | slack_button | O only | gate flips to approved; O forwards the draft manually. No auto-channel write. |

Every SLT-facing deliverable (daily, Friday, on-demand forecast/movers/scorecards) lands only in O's DM. O forwards to Henry / Anand / Hutch via copy-paste. Phase 1 intentionally has zero auto-post to any SLT channel — locked 2026-04-13.

## Forecast scorer — 5 pillars

Pillar weights (v1-seed): ICP **0.25**, Stage **0.30**, Activity **0.15**, Timeline **0.15**, Call Intel **0.15**. Rep performance is a data column on the AE scorecard, not a composite weight. ICP null proxies cap at **0.5** (see `pipeline/config.py::ICP_PROXY_CAP`).

Bands (LUCID-compatible, inclusive of lower bound): Strong Commit ≥80 → 80% prob, Commit ≥60 → 50%, High Confidence ≥40 → 22%, Longshot ≥20 → 10%, Pipe Dream <20 → 3%.

Weights live in `forecast_history.metadata.weights` per run. To tune, use `@oo slt weights set <pillar>=<val>` — requires explicit approval via the standard gate. Replay history by re-running `forecast/backtest.py` against the desired `weights_version`.

## Excel workbook

9 sheets produced by `excel_model/builder.py` from a single `RevenueModelPayload`:

1. Deal Details · 2. AE Scorecard · 3. SDR Scorecard · 4. Unit Economics · 5. Quota · 6. Pipeline by Segment · 7. Deal Movers · 8. Forecast Summary · 9. Board Metrics

BigQuery unit-economics is gap-flagged by default — cells render `-- (Loop Pulse unavailable)` until `BQ_CREDENTIALS_JSON` is wired. Every sheet continues to render; the Unit Economics sheet surfaces the gap explicitly.

Output: `$REVOPS_REPO_ROOT/var/reports/revenue_model/<YYYY-MM-DD>/Loop_Revenue_Model_<YYYY-MM-DD>.xlsx`. When `GDRIVE_FOLDER_ID` + `GDRIVE_SERVICE_ACCOUNT_JSON` are set, `gdrive/uploader.py` publishes to the folder and returns a shareable link — included in the Slack briefing. Missing env → warning in O's DM + local path surfaced instead.

## Data model

| Table | Purpose |
|---|---|
| `pipeline_snapshots` | daily (date, opp_id) rows — stage/acv/close_date/score/probability/weighted_acv/metadata. Append-only, `ON CONFLICT DO NOTHING` keyed on `(snapshot_date, opp_id)`. |
| `forecast_history` | one row per forecast run — weights_version / commit / best_case / weighted / backtest metrics. Append-only; `actuals_at_close` + `accuracy_pct` backfilled post-quarter. |

Migration: `shared/db/migrations/versions/0004_slt_revenue_metrics.py`.

## Integrations

- **Salesforce MCP** — primary fetch (`shared/mcp/salesforce_mcp.soql_query`). `ICP_Score__c` optional; scorer falls back to capped proxy when absent.
- **Fireflies MCP** — call-intel pillar (`list_transcripts`, `get_transcript`). Top-20 ACV opps optionally scored by Haiku classifier when keyword signal lands in [0.4, 0.6].
- **BigQuery (Loop Pulse)** — deferred. Gap-flag path is the default; swap env vars in and `unit_economics` and `board_metrics` light up without code changes.
- **GDrive** — workbook upload via service-account. Missing env → local path returned with a warning.

## Dependencies

Declared in `pyproject.toml`: `openpyxl`, `anthropic`, `slack-sdk`, `slack-bolt`, `sqlalchemy`. `google-api-python-client` + `google-auth` load lazily in `gdrive/uploader.py`; install before flipping `GDRIVE_FOLDER_ID`.
