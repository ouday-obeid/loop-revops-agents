# Outbounder — Loop-agent Activation Audit

**Date:** 2026-04-20 (Monday)
**Auditor:** JARVIS (Claude Opus 4.7)
**Scope:** Outbounder = the `top_of_funnel` Loop agent reachable via `@oo outbounder …`. JARVIS-side `~/JARVIS-HUB/OUTBOUNDER/` stack was decommissioned earlier today.
**Subject:** `/Users/ottimate/loop-revops-agents/agents/top_of_funnel/`

---

## Executive verdict

**Outbounder is OPERATIONAL BUT STARVED.** Every runtime surface is alive and executing as designed. The plumbing — daemon, dispatcher, alias, handler, enrichment cron, briefing cron, stale-guard, state DB — is wired correctly and tested. **The only thing stopping Outbounder from actively supporting ToF today is that `APOLLO_API_KEY` and `CLAY_API_KEY` are still `REPLACE` in `.env`.** Without them, the pipeline scans 0 leads daily → briefing has nothing to send → stale-guard fires → SDRs never DMed.

Three ToF outcomes, graded:

| Outcome | Grade | Evidence |
|---|---|---|
| **1. Pipeline enriches new leads daily** | ⚠️ AMBER | Cron fires 02:00 Mon–Fri on MBP ✅. Today's run: `run-20260420-060005-7bb7e6`, `status=success`, `scanned=0`. **Zero real work because Apollo/Clay keys unset.** Graceful degrade, not a crash. |
| **2. SDRs receive daily briefings** | ❌ RED | Cron fires 07:55 Mon–Fri on Mini ✅. Today's run: stale-guard tripped (pipeline completed 5h 55min before, >4h threshold). O correctly DMed with skip reason. **SDRs got nothing.** Downstream of outcome #1. |
| **3. `@oo outbounder` responds in Slack** | ✅ GREEN | Live round-trip test 2026-04-20 22:38:22 → 22:38:23 EDT (**1 second**, under 2s SLO). Help returned correct `top_of_funnel` subcommand list. Alias routing confirmed. |

---

## Runtime surface — detailed check

### OO daemon + `@oo outbounder` dispatch
| Check | Result |
|---|---|
| `com.loop-revops.oo-daemon` MBP | ✅ PID 753, last Socket Mode session 20:34 today |
| `com.loop-revops.oo-daemon` Mini | ✅ PID 2732, last session 20:16 today |
| `app_mention` → dispatch → alias → handler round-trip | ✅ **1s live** (ts `1776739103.756469` proves it) |
| `outbounder → top_of_funnel` alias map | ✅ `shared/slack_dispatcher.py:28-35` |
| Subcommands accepted | ✅ `ping`, `enrich`, `score`, `daily`, `suppress`, `queue`, `credits`, `help` |
| Dev guard redirecting replies | ✅ `SLACK_DEV_GUARD=1`, replies land in `#revops-agents-test` (C0AS690G04X) |
| Test suite | ✅ **236 passed, 6 skipped in 1.05s** |

### ToF enrichment pipeline (Mon–Fri 02:00)
| Check | Result |
|---|---|
| Plist scheduled | ✅ `com.loop-revops.top-of-funnel-enrichment-pipeline.plist` Mon–Fri 02:00 on both hosts |
| Fires on MBP | ✅ Last run today 02:00, err log 100B |
| Fires on Mini | ⚠️ Fires but **crashes on missing schema** — state.db is 0 bytes, no tables |
| Apollo integration | ❌ `APOLLO_API_KEY=REPLACE` → pipeline logs `apollo_unavailable` and short-circuits (`scanned=0`) |
| Clay integration | ❌ `CLAY_API_KEY=REPLACE` → Clay waterfall unavailable |
| MBP state.db health | ✅ 1.7MB, 7 tables present, 10 historical runs, 298 stale `status='ready'` candidates from Apr 16 testing, 397 Apollo cache rows (from when key was valid) |
| Clay credit ledger | 298/50000 consumed in 2026-04 (all pre-key-rotation) |

### ToF daily briefing (Mon–Fri 07:55)
| Check | Result |
|---|---|
| Plist scheduled | ✅ `com.loop-revops.top-of-funnel-daily-briefing.plist` Mon–Fri 07:55 on both hosts |
| Fires on MBP | ❌ Log 0B since Apr 17 (system likely sleeping at 07:55) |
| Fires on Mini | ✅ Today 07:55 fired, err log 9.5KB (same schema crash as pipeline) |
| **Actual dispatch** | ✅ Post at 07:55:06 EDT today in `#revops-agents-test`: `ToF Daily Briefing skipped — pipeline_stale` → **the briefing IS running, on Mini, via a different path** |
| Stale-guard design | ✅ 4h threshold; DMs O with reason when tripped; no 0-lead SDR spam |
| Today's skip reason | `pipeline_stale (run_id=run-20260420-060005-7bb7e6, completed=2026-04-20T06:00:05Z, now=2026-04-20T11:55:05Z)` — **elapsed 5:55 > 4:00 threshold** |
| SDR DM payload | Would carry up to 20 primary + 20 exploration slot leads per SDR + Hutch summary; none today (0 `ready` candidates for today's run) |

### Ancillary signals observed in `#revops-agents-test`
- 08:30 full OO daily briefing (different codepath — dispatcher-level briefing; does post)
- 09:00 integration-health alerts (false `momentum degraded` due to `sf` CLI not in launchd PATH)
- 09:00 knowledge-refresh diff (SF schema tracker)
- 11:00 CS weekly digest (Agent 4 specialist)
- Weekly review: posted Fri 2026-04-17 16:00 ($0 commit, $0 best — week was light)

---

## What's blocking Outbounder from actual ToF work

### 🔴 #1. Apollo API key unset
**Where:** `/Users/ottimate/loop-revops-agents/.env:108` — `APOLLO_API_KEY=REPLACE`
**Effect:** Pipeline logs `apollo_unavailable: APOLLO_API_KEY not configured` at every 02:00 run; graceful degrade to `scanned=0`. **Nothing new enters the funnel.**
**Fix:** Provision key (prod Apollo account). Drop into `.env`, restart daemon + reload the enrichment-pipeline plist (or wait for tomorrow's 02:00 run — no restart required; launchd reads env fresh each run).

### 🔴 #2. Clay API key unset
**Where:** `/Users/ottimate/loop-revops-agents/.env:100` — `CLAY_API_KEY=REPLACE`
**Effect:** Integration-health flags `clay: degraded — no api key configured`. Pipeline can't run Clay waterfall even if Apollo produces candidates.
**Fix:** Same as #1. Note monthly budget is already set (`CLAY_MONTHLY_BUDGET_CREDITS=50000` in `.env:102`).

### 🔴 #3. Mini state.db schema never applied
**Where:** `/Users/jarvis/loop-revops-agents/agents/top_of_funnel/state.db` on Mini — 0 bytes.
**Effect:** Every 02:00 enrichment + every 07:55 briefing crashes immediately with `no such table: tof_enrichment_runs`. Mini adds no value, and the err logs grow by ~10KB every morning.
**Cause:** Bootstrap script `infra/bootstrap.sh` was never run on Mini (state.py docstring misreferences it as `scripts/init_agent_state.sh` which doesn't exist).
**Fix:** `ssh mini; cd ~/loop-revops-agents; .venv/bin/python -c "from sqlalchemy import text; from agents.top_of_funnel.state import get_state_engine; e=get_state_engine(); sql=open('agents/top_of_funnel/state.sql').read(); import sqlite3; conn=sqlite3.connect('agents/top_of_funnel/state.db'); conn.executescript(sql); conn.commit()"`
**Alternative:** unload Mini ToF plists until the schema is applied (prevents daily crash log noise; MBP covers).

### 🟡 #4. Integration-health `sf` CLI PATH bug
**Where:** `var/launchd/com.loop-revops.oo-integration-health.plist` on both hosts.
**Effect:** Launchd's default PATH excludes `/opt/homebrew/bin`. `sf` CLI invoked during the `salesforce`/`momentum` health checks returns `[Errno 2] No such file or directory: 'sf'`. Result: false degraded alerts every 30 min (observed 09:00 today and in every daily briefing).
**Fix:** Add `<key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>` to the plist's `EnvironmentVariables` dict.

### 🟡 #5. Pipeline timing vs briefing stale-guard
**Where:** Pipeline runs 02:00, briefing runs 07:55, threshold is 4h. Gap is 5:55 → briefing always stale-guards today because the pipeline short-circuits in < 1s.
**Effect:** Even after fixing Apollo/Clay, if the pipeline completes in < 2h, the briefing still stale-guards.
**Options:**
- Move pipeline to 04:00 (gap 3:55, under threshold)
- Widen threshold to 6h (accept somewhat older data)
- Require briefing to check the most-recent *successful-with-data* run, not just most-recent
**Recommendation:** Move pipeline to 04:00 Mon–Fri — single-file plist change, matches SDR expectations (briefings land before the 08:30 standup).

### 🟢 #6 (already resolved). Handler, dispatcher, tests
- Handler accepts all 8 subcommands; `_cmd_help` returns correct menu.
- Dispatcher's `parse_command` strips `<@U...>` mentions and resolves `outbounder → top_of_funnel`.
- 236 ToF tests pass locally in 1s. No pytest skips that point to infra gaps.

---

## Priority-ranked remediation

| Order | Fix | Effort | Impact |
|---|---|---|---|
| 1 | Provision **APOLLO_API_KEY** | 10 min (paste into `.env`) | Unblocks daily enrichment — #1 lever for Outbounder supporting ToF |
| 2 | Provision **CLAY_API_KEY** | 10 min | Unblocks waterfall enrichment + resolves `clay: degraded` alert |
| 3 | Move pipeline to **04:00** in plist | 5 min | Ensures briefing isn't stale-guarded even on fast pipeline runs |
| 4 | Apply schema on Mini (or unload its ToF plists) | 5 min | Stops daily crash log noise on Mini |
| 5 | Fix `sf` CLI PATH in integration-health plist | 5 min | Stops false `momentum degraded` alerts in daily briefings |

Total: **~35 min of config work.** No code changes needed.

---

## What works TODAY — don't touch

- OO daemon Socket Mode on both hosts
- Dispatcher + `outbounder → top_of_funnel` alias
- Handler subcommands + help text
- ToF test suite (236 pass)
- MBP state.db + 7 tables
- Stale-guard logic (it correctly prevented sending a 0-lead briefing today)
- Dev-guard redirect (replies go to `#revops-agents-test`, prod channels protected)
- Live dispatch proof: ts `1776739103.756469`

---

## What I did not do (out of scope / deferred)

- Did **not** provision Apollo or Clay keys — that's O's call (billing + credential management).
- Did **not** run Mini schema bootstrap — would technically be a live config change; want sign-off.
- Did **not** move pipeline plist to 04:00 — wait for O's preference.
- Did **not** fix `sf` PATH — same rationale.
- Did **not** unload Mini ToF plists — harmless for now (crash-log noise only).
- Did **not** audit other specialists (closer/onboarder/supporter/admin/urkel).

---

## Critical paths for the fix

| Fix | Path |
|---|---|
| Apollo/Clay keys | `/Users/ottimate/loop-revops-agents/.env:100,108` |
| Mini schema bootstrap | `ssh mini` → run schema against `~/loop-revops-agents/agents/top_of_funnel/state.db` using `agents/top_of_funnel/state.sql` |
| Pipeline timing | `/Users/ottimate/loop-revops-agents/var/launchd/com.loop-revops.top-of-funnel-enrichment-pipeline.plist` (change `<key>Hour</key><integer>2</integer>` × 5 to `4`) |
| `sf` PATH | `/Users/ottimate/loop-revops-agents/var/launchd/com.loop-revops.oo-integration-health.plist` EnvironmentVariables |

---

## Raw evidence

- Live dispatch ts: `1776739103.756469` in `#revops-agents-test` (C0AS690G04X) — 1s round-trip
- Today's ToF briefing skip: `1776686106.080149` in `#revops-agents-test` — stale-guard fired at 07:55:06 EDT
- Latest enrichment run on MBP: `run-20260420-060005-7bb7e6`, success, scanned=0
- Historical runs on MBP: 10 recorded, all scanned=0 (consistent with Apollo being unset since Apr 16)
- 298 `ready` candidates on MBP from Apr 16 16:53 test batch — **stale, will never be briefed** because they belong to old `run_id`s
- Test suite: `236 passed, 6 skipped, 715 warnings in 1.05s`
