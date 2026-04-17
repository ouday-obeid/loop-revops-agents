# Resume prompt — Urkle / PC-bot merge

Paste this into a new Claude Code session on MacBook Pro (primary host for this work).

---

We're resuming the Urkle (slt_metrics) PC-bot merge. I'm on MacBook Pro. Mini auto-syncs; don't edit on Mini directly.

**Ground truth**
- Branch: `feat/slt-metrics-pc-bot-merge` at commit `2d9fd79`, pushed to `mini` remote. Origin push failed previously (GitHub auth) — retry if you need it.
- Project: `/Users/ottimate/loop-revops-agents/`
- Full breadcrumb lives in memory under `urkle_pc_bot_merge.md` (and `loop_revops_agents.md` for project-wide context). Read both first.

**What's done**
- `agents/slt_metrics/pipeline/planning.py` — 2026 annual/monthly/segment/blended/quarterly-funnel/rates/seasonality/headcount + 16 AE + 14 SDR roster + manager groupings.
- `scripts/seed_slt_rep_config.py` — idempotent upsert into `rep_config`.
- 24 tests passing (19 planning + 5 seed). Full suite green at 433.

**What's next (priority order, from memory)**
1. Extend `RevenueModelPayload` with `closed_opps_quarter: list[OppRecord]` + `all_opps_snapshot: list[OppRecord]`. Thread through `pipeline/fetcher.py` + `jobs.run_morning_snapshot`. Tests.
2. Add `agents/slt_metrics/excel_model/aggregates.py` with: `monthly_closed_won_by_kind`, `monthly_opps_created`, `stage_distribution`, `quarterly_closed_by_segment`, `lead_source_summary`.
3. Add 4 new sheets under `agents/slt_metrics/excel_model/sheets/`: `expansion.py`, `monthly_revenue.py`, `funnel_metrics.py`, `rep_forecast.py`. Register in `builder.py::_LATE_SHEET_FACTORIES` (9→13). Legacy reference at `C:\Users\odayo\revenue_model\sheets\` on Gaming PC — adapt from pandas to `RevenueModelPayload` contract; don't straight-port.
4. Rep-forecast ingest: `@oo slt ingest-rep-forecast <file>` + storage (table or `var/rep_forecast/`).
5. Wire existing Urkle sheets (quota, ae_scorecard, sdr_scorecard, forecast_summary, board_metrics) to read `planning.py` constants.
6. Run `python scripts/seed_slt_rep_config.py` on MacBook Pro dev DB, then Mini prod DB once sheets need it.
7. Install launchd plists on Mini: `com.loop-revops.slt-morning-snapshot` + `com.loop-revops.slt-daily-briefing`. Only `slt-friday-review` is loaded today.
8. Archive PC bot to Mini: `/Users/jarvis/loop-revops-agents/var/legacy/revenue_model_<date>/`. Write `DEPRECATED.md`. Then `schtasks /Delete /TN "RevUpdater SlackBot"` + `rm -rf C:\Users\odayo\revenue_model\` on PC. **Requires Loop AI passphrase.**
9. E2E smoke: `run_morning_snapshot()` → `run_daily_briefing()` → confirm `slt_draft_review` gate lands in my DM.

**Architectural reminders**
- Pure-data config only in `pipeline/` — no I/O, no DB.
- All SF writes route through `shared.governance`.
- No cross-agent imports (`agents/<a>/` must not import `agents/<b>/`).
- No hardcoded `/Users/ottimate/` or `/Users/jarvis/` in `shared/` or `agents/`.
- New migrations increment from `0006_approval_gates_approvals` — so next would be `0007_*`.

**First commands to run**
```bash
cd /Users/ottimate/loop-revops-agents
git checkout feat/slt-metrics-pc-bot-merge
git status
source .venv/bin/activate
python -m pytest agents/slt_metrics/tests/ -q  # expect 433 green before touching anything
```

Start with step 1 (payload extension). Propose the diff before writing the code — I want to see the shape change before the callers ripple out.
