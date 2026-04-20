# DEPRECATED — Loop Revenue Model (legacy PC bot)

**Archived:** 2026-04-17
**Replaced by:** `agents/slt_metrics/` in this repo
**Original location:** `C:\Users\odayo\revenue_model\` on the Windows Gaming PC (`pc1`, Tailscale `100.114.136.70`) — deleted 2026-04-17 after this archive committed
**Original scheduled task:** `RevUpdater SlackBot` (Windows Task Scheduler) — deleted 2026-04-17

## Why this code is here

This is the canonical snapshot of the OUTBOUNDER / RevUpdater revenue-model
generator that ran on the Gaming PC before `agents/slt_metrics/` was built.
Preserved as a historical reference for:

- Planning constants (monthly ramp, segment targets, seasonality, roster quotas)
  → ported to `agents/slt_metrics/pipeline/planning.py` (`2d9fd79`)
- 11 Excel sheets (forecast, expansion, monthly revenue, funnel metrics,
  rep forecast, AE/SDR scorecards, pipeline analysis, segment analysis,
  monthly unit economics, dashboard) → modern versions under
  `agents/slt_metrics/excel_model/sheets/` (`0d73dd8`, `2556d73`)
- Slack bot command surface (`slack_bot/app.py`, movers/digest/generator)
  → superseded by `@oo slt …` dispatcher in `agents/slt_metrics/dispatcher.py`

## What's NOT here

- **`slack_bot/.env`** — excluded from the archive; held live Slack tokens
  that are being rotated into the modern agent's secret surface
- **`.git/`** — this code lived in its own repo on the PC; not preserved
- **`__pycache__/`** — rebuilt on every run; no signal

## Where things moved

| Legacy path | Modern equivalent |
|-------------|-------------------|
| `config.yaml` → planning constants | `agents/slt_metrics/pipeline/planning.py` |
| `core/deal_matcher.py` | `agents/slt_metrics/pipeline/movers.py` + `forecast/commit_best.py` |
| `core/forecast_loader.py` | `agents/slt_metrics/pipeline/rep_forecast_store.py` + `pipeline/rep_forecast_parser.py` |
| `core/loader.py` | `agents/slt_metrics/pipeline/fetcher.py` |
| `core/roster_loader.py` | `agents/slt_metrics/pipeline/planning.py::AE_ROSTER,SDR_ROSTER` + `scripts/seed_slt_rep_config.py` |
| `core/processor.py` | `agents/slt_metrics/excel_model/aggregates.py` + `forecast/scorer.py` |
| `sheets/expansion.py` | `agents/slt_metrics/excel_model/sheets/expansion.py` |
| `sheets/monthly_revenue.py` | `agents/slt_metrics/excel_model/sheets/monthly_revenue.py` |
| `sheets/funnel_metrics.py` | `agents/slt_metrics/excel_model/sheets/funnel_metrics.py` |
| `sheets/rep_forecast.py` | `agents/slt_metrics/excel_model/sheets/rep_forecast.py` |
| `sheets/forecast.py` | `agents/slt_metrics/excel_model/sheets/forecast_summary.py` |
| `sheets/ae_scorecard.py`, `sdr_performance.py` | `agents/slt_metrics/excel_model/sheets/ae_scorecard.py`, `sdr_scorecard.py` |
| `sheets/segment_analysis.py`, `pipeline_analysis.py` | `agents/slt_metrics/excel_model/sheets/pipeline_segment.py` |
| `sheets/monthly_unit_economics.py` | `agents/slt_metrics/excel_model/sheets/unit_economics.py` |
| `sheets/dashboard.py` | `agents/slt_metrics/excel_model/sheets/board_metrics.py` |
| `slack_bot/app.py` + `__main__.py` | `agents/slt_metrics/dispatcher.py` via OO daemon |
| `slack_bot/digest.py` | `agents/slt_metrics/briefings/daily_830.py`, `friday_review.py` |
| `slack_bot/movers.py` | `agents/slt_metrics/pipeline/movers.py` |
| `slack_bot/generator.py` | `agents/slt_metrics/excel_model/builder.py` |
| `slack_bot/data/yesterday.json` | `pipeline_snapshots` table (migration 0004) |
| `slack_bot/data/roster.xlsx` | `rep_config` table (migration 0005) + `AE_ROSTER` / `SDR_ROSTER` |
| `slack_bot/data/forecast.xlsx` | `rep_forecasts` table (migration 0007) via `@oo slt ingest-rep-forecast` |
| `RevUpdater SlackBot` Windows task | launchd jobs `com.loop-revops.slt-morning-snapshot`, `…slt-daily-briefing`, `…slt-friday-review` on Mac Mini |

## Path-choice note

The resume-prompt originally specified `var/legacy/revenue_model_<date>/`
for this archive. That path is gitignored in this repo, which would leave
the snapshot untracked and unable to travel across machines. Archive was
placed at the repo root under `legacy/` instead, committed to the
`feat/slt-metrics-pc-bot-merge` branch so it rides along with the
agent code that supersedes it.

## Do not revive

The PC bot reads from an offline `revenue_model.xlsx` + `roster.xlsx` and
writes to Slack via Socket Mode. Both paths have modern equivalents. If
something here looks useful, port it into `agents/slt_metrics/` — do not
re-enable the original code or re-register the Windows scheduled task.
