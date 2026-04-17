# Top of Funnel Agent Runbook

## Deploy
Phase 0 bootstrap must be green first.
```bash
cd $REVOPS_REPO_ROOT
source .venv/bin/activate
python -c "from shared.db.connection import get_engine; from sqlalchemy import text; open('agents/top_of_funnel/state.sql').read().split(';') and [conn.execute(text(s)) for s in open('agents/top_of_funnel/state.sql').read().split(';') if s.strip() for conn in [get_engine().begin().__enter__()]]"  # one-time: apply state.sql
bash infra/install_launchd.sh  # picks up the 2 new rows in shared/runtime/schedule.py
```

## Register with OO dispatcher
`agents/top_of_funnel/main.py:register_with_dispatcher` is called by `agents/oo/main.py` at startup. If OO is already running, restart the daemon:
```bash
launchctl kickstart -k gui/$UID/com.loop-revops.oo-daemon
```

## Pause
```bash
launchctl bootout gui/$UID/com.loop-revops.top-of-funnel-enrichment-pipeline
launchctl bootout gui/$UID/com.loop-revops.top-of-funnel-daily-briefing
```
This halts cron but leaves the Slack handlers live. To fully sever, comment the two `register(...)` calls in `main.py` and restart OO.

## Rollback
1. Pause (above).
2. Drop plists: `rm ~/Library/LaunchAgents/com.loop-revops.top-of-funnel-*.plist`
3. Remove cron entries from `shared/runtime/schedule.py` (revert the Phase 0 amendment).
4. Optional wipe of agent state: `rm agents/top_of_funnel/state.db` (destroys suppression cache + Clay ledger + candidate history; audit trail in shared DB remains).

## Logs
- Cron stdout/stderr: `$REVOPS_REPO_ROOT/var/log/top-of-funnel-*.{out,err}.log`
- Slack dev channel: `#agent-tof-log`
- DB: `audit_log` (every SF write), `agent_runs` (every run), `tof_enrichment_runs` (per-account summary)

## Force a run now
```bash
cd $REVOPS_REPO_ROOT && source .venv/bin/activate

# Enrich one domain (no SDR briefing, no queue, audit only):
python -c "import asyncio; from agents.top_of_funnel.enrichment.pipeline import enrich_single; print(asyncio.run(enrich_single('example-franchise.com')))"

# Full pipeline dry-run (no SF writes, no approval gate):
python -c "import asyncio; from agents.top_of_funnel.enrichment.pipeline import run_pipeline; print(asyncio.run(run_pipeline(dry_run=True)))"

# SDR briefing preview to O's DM (NOT to real SDRs — SLACK_DEV_GUARD=1 pins to test channel):
python -c "import asyncio; from agents.top_of_funnel.daily_briefing import send_dry_run; print(asyncio.run(send_dry_run('U08K2UTG3G8')))"
```

## Threshold tuning
All knobs live in YAML — no code change required.
- **ICP tier bands** — edit `config/icp_config.yaml` (`tier_a_min`, `tier_b_min`, `exploration_min`); restart not required, YAML is read per-run.
- **Clay budget** — edit `CLAY_MONTHLY_BUDGET_CREDITS` in `.env`; 80% alert/100% block thresholds hard-coded in `clay_client.py`.
- **Grade-floor override** (let Grade C through for one campaign) — edit `config/icp_config.yaml:clay_grade_floor` (default `B`).
- **Rate limits** — edit `shared/governance.py:RATE_LIMITS` (shared change — ping Phase 0 owner first).
- **Lead fallback field** — `TOF_LEAD_FALLBACK_FIELD` in `.env` picks the long-text field that carries packed ICP/Brand/Ownership metadata when the real custom fields are absent. Default `Description`. Set to `""` (empty) on sandboxes without a Description to disable the fallback — writes keep working, only the packed metadata is dropped.
- **Lead TLO field** — `TOF_LEAD_TLO_FIELD` in `.env` picks the Lead-side reference field that receives the Top_Level_Organization__c Id. Default `Top_Level_Organization__c` (Loop's sandbox + prod convention, matches Opportunity). Override to `Top_Level_Org__c` only if prod's Lead diverges to Account's short-name convention.

## Handling Clay 80% alert
1. Alert posts to `#agent-tof-log` with current usage + remaining credits.
2. Decide: (a) let the month play out, (b) top up Clay, (c) raise `clay_grade_floor` to `A` to slow burn.
3. If topping up: update `CLAY_MONTHLY_BUDGET_CREDITS` in `.env` and restart OO daemon.

## Handling Clay 100% block
Enrichment hard-stops with `ClayBudgetExceeded`. SF lead creation pauses automatically (pipeline can't complete without enrichment). Actions:
1. Top up credits + bump budget env var.
2. Restart OO daemon.
3. Kick pipeline manually (see **Force a run now**).

## False-positive playbook
An SDR flags a lead as bad fit (reply in briefing thread).
1. `@oo tof suppress <email> <reason>` → writes to `suppression_cache`.
2. Future runs exclude this domain for 90 days.
3. If the reason is systemic (e.g., ICP drift), file a ticket to recalibrate weights at next quarterly review.

## Incident: sequence rate-limit burst
`RateLimitExceeded` at the 51st enrollment of the day.
1. The offending call writes an audit row; `sequence_enroller` halts cleanly.
2. Post a summary to `#agent-tof-log` showing which SDR(s) were mid-queue.
3. Resume next morning at 08:00 review window — the bucket resets at midnight UTC.
4. If this recurs >2 days in a week, raise `RATE_LIMITS["nooks_sequences_daily"]` with O approval.

## Incident: pipeline missed 02:00 run, briefing at 07:55 has nothing fresh
Briefing's stale-pipeline guard triggers: instead of spamming SDRs with 0-lead DMs, it DMs O with "pipeline last completed X hours ago". Actions:
1. Check `agent_runs` for the failing run; read stderr log.
2. If transient (Apollo 502, etc.) → force-run the pipeline; the briefing job can be re-fired manually (see **Force a run now**).
3. If persistent → pause and file a ticket.

## Known risks
- **Phase 0 `single_record_update` quirk.** `require_approved_gate` raises even for the `auto_notify` tier. Mitigation: one batch gate per pipeline run; passed to every `create_record` call. Flag to Phase 0 team if Phase 2 wants per-lead gates.
- **Schema drift.** If `ICP_Score__c` / `Brand__c` / `Ownership_Type__c` are added or renamed, the schema probe on startup detects and switches to real fields. The old `Description`-packed rows stay as-is (not backfilled) — ok because they're all <30 days old by the time the schema lands.
- **SDR absence.** `territory.yaml` should mark inactive users; `routing.py` skips them and falls back to `DEFAULT_OWNER_ID`. Verify quarterly.
- **219-brand universe exhausts in ~5 days.** Briefing gracefully reports "fully canvassed this cycle"; re-engagement logic deferred to Agent 5/6 Phase 2.
- **`tof-agent@tryloop.ai` SF user.** Must exist w/ Lead Create permset before D6 sandbox integration. O creates during D1 kickoff.
- **Nooks cadence SF object.** Confirm with O at D1 kickoff; exact object name goes in `.env` as `NOOKS_CADENCE_SF_OBJECT`. Likely `CampaignMember`, `Task`, or a Nooks-specific custom object.

## Escalation
- Non-urgent: post to `#agent-tof-log`
- Urgent (pipeline down, Clay bill >$X, schema drift): DM O at `U08K2UTG3G8`
- Dept-head access: emails in `config/territory.yaml:dept_heads` (Hutch, Charles). `routing.is_dept_head(email)` is the hook downstream callers use when gating `@oo tof` commands beyond O-only.

## Cross-agent import lint
This agent must not import from other agents (only `shared.*`). Verify before ship:
```bash
grep -rE "from agents\.(?!top_of_funnel)" agents/top_of_funnel/ --include="*.py" || echo "clean"
```
