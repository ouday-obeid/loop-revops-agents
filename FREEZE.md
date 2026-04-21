# FREEZE — Non-ToF agents paused 2026-04-21

**Directive:** Loop CEO (Anand) instructed O to focus only on lead generation / Top of Funnel (Outbounder) going forward. Horizon is indefinite.

**Effective date:** 2026-04-21

**Scope of freeze:** Every agent in this repo EXCEPT `agents/top_of_funnel/` and its shared dispatcher/ooplumbing (`agents/oo/`, `shared/`, `config/`).

**What "frozen" means:**
- No new feature development, no new tests, no refactors.
- No new branches, no merges to paused agent trees.
- Existing code remains in `main` untouched — preservation only.
- Existing launchd jobs remain loaded on MacBook Pro + Mac Mini (see "Launchd status" below). They may log errors or exit non-zero due to starved `.env` values; that is acceptable background noise during freeze.
- Worktrees on disk are left intact for now. Decision to prune deferred.
- Genuine production fires → patch minimally, do not extend scope.

**What is NOT frozen (actively being built / run):**
- `agents/top_of_funnel/` — Outbounder, the Loop agent
- `agents/oo/` — dispatcher + briefings + board monitor (transport layer; ToF depends on it)
- `shared/` — cross-cutting utilities
- `config/` — runtime configuration

---

## Paused agents — last-good state as of 2026-04-21

Reference HEAD: `main @ d788138` = tag `v0.10-slt-pc-bot-merge`.

| Agent | Dir | Last main commit touching dir | Latest release tag | Freeze tag (local) | Launchd jobs (on Mini + MBP) |
|-------|-----|------------------------------|---------------------|---------------------|-------------------------------|
| **A2 — Sales Reps** | `agents/sales_reps/` | `e910b97` | `v0.4-sales-reps` | `v0.10-frozen-a2-2026-04-21` | 7 jobs: `sales-reps-brief-scan`, `-grader-poll`, `-hygiene-daily`, `-leaderboard-weekly`, `-risk-sweep`, `-scorecards-weekly`, `-sync-check` |
| **A3 — Onboarding** | `agents/onboarding/` | `1e80cb9` | `v0.2-onboarding` | `v0.10-frozen-a3-2026-04-21` | 4 jobs: `onboarding-closed-won-poller`, `-jackie-digest`, `-location-sweep`, `-milestone-monitor` |
| **A4 — CS** | `agents/cs/` | `1e80cb9` | (no dedicated tag) | `v0.10-frozen-a4-2026-04-21` | 7 jobs: `cs-churn-sweep`, `-expansion-scan`, `-health-poll`, `-integration-health`, `-renewal-pipeline`, `-renewal-stall`, `-weekly-report` |
| **A5 — Admin / RevOps Support** | `agents/revops_support/` | `e910b97` | `v0.9.2-phase1-residuals` | `v0.10-frozen-a5-2026-04-21` | 3 jobs: `revops-support-cooldown-poller`, `-metadata-digest`, `-metadata-refresh` |
| **A6 — SLT Metrics** | `agents/slt_metrics/` | `084819e` | `v0.10-slt-pc-bot-merge` | `v0.10-frozen-a6-2026-04-21` | 3 jobs: `slt-daily-briefing`, `-friday-review`, `-morning-snapshot` |
| **A7 — Conductor** | (no `agents/` dir; artifacts in `conductor/prereqs` worktree only) | n/a | `v0.8-conductor-prereqs` | `v0.10-frozen-a7-2026-04-21` | (none; Stage 1 never fired) |

Total paused launchd jobs: **24**. All remain loaded. None are being disabled today.

---

## Worktrees on disk (deferred decision)

Six feature worktrees remain checked out under `/Users/ottimate/`. They are NOT being merged or removed. Listed here for inventory only:

| Worktree | Branch | HEAD | Status |
|----------|--------|------|--------|
| `loop-revops-agents-amendment` | `tof/amendment-memo-cleanup` | `5764c1b` | ToF-related — retain, may revisit |
| `loop-revops-agents-conductor-prereqs` | `conductor/prereqs` | `3edd0b0` | A7 freeze scope — leave dormant |
| `loop-revops-agents-scenarios` | `tests/phase1-scenarios` | `30fdb0a` | Cross-cutting Phase 1 tests — leave dormant |
| `loop-revops-agents-schema-fixes` | `sales-reps/org-schema-fixes` | `08da9bf` | A2 freeze scope — leave dormant |
| `loop-revops-agents-slt-forecast` | `fix/slt-forecast-dispatcher` | `b2c6630` | A6 freeze scope — leave dormant |
| `loop-revops-agents-slt-metrics` | `slt-metrics/d2-d15-build` | `0d73dd8` | Already merged into main as v0.10 — safe to `git worktree remove` when O says |
| `loop-revops-agents-tof-polish` | `tof/sandbox-field-polish` | `85c6c1e` | ToF-related — retain, may revisit |

---

## Active branches (non-worktree) — dormant during freeze

No action required; listed for completeness. See `git branch -a` for canonical list. Notable non-ToF branches: `admin/phase-1.5`, `admin/v0.9.1-hotfix`, `cs/cli-plan-mode-entry`, `feat/slt-metrics-pc-bot-merge` (merged), `sales-reps/d1-d11-build`, `sales_reps/four-fixes`, `shared/fireflies-mcp-smoke`, `shared/governance-dual-approval`, `shared/knowledge-mcp-seed`, `shared/salesforce-mcp-coverage`, `shared/slack-dispatcher-approval-thread`, `slt-metrics/test-isolation-fix`, `top-of-funnel/d6-d10-build`.

---

## Resume conditions

If Anand reverses direction or scope is expanded back to the 7-agent system:

1. Read this file first. Every frozen agent has a freeze tag — start from there.
2. Review `git log main -- agents/<agent>/` since freeze tag for any hotfix changes that landed during freeze.
3. Check Monday board 18408463906 for any blocker comments added during freeze.
4. Restart any launchd jobs that were disabled (none as of freeze date; update this section if that changes).
5. Update this file with a "Resumed YYYY-MM-DD" entry at the bottom.

---

## Related docs

- **Pivot plan:** `~/.claude/plans/few-changes-on-everything-serialized-ladybug.md`
- **Outbounder audit:** `~/loop-revops-agents/AUDIT_OUTBOUNDER_2026_04_20.md` (5 Phase 1 fixes — API keys, cron shift, state.db bootstrap, sf CLI PATH)
- **Outbounder skill:** `~/.claude/skills/outbounder-enrich-pipeline/SKILL.md`
- **Dispatcher runtime:** `agents/oo/` (shared infrastructure; NOT frozen)

---

## History

- 2026-04-21 — Freeze initiated per CEO directive. All 24 non-ToF launchd jobs left loaded; six freeze tags created locally (not pushed). No code deletions. No Monday-board stamping yet (awaiting O approval).
