# Backport from Blaine's repo

Blaine owns `loop-cs-agents` (Onboarder + Supporter). This doc describes how her work lands back in this repo when you want the Mac Mini running her improvements instead of the frozen extract snapshot.

## The model

- `loop-revops-agents` (here) is **upstream authority.** Nothing auto-merges.
- `loop-cs-agents` is her **fork.** She ships at her own pace.
- You decide commit-by-commit what flows back. The Mini keeps running upstream, so until you backport, her improvements aren't in your cron.

## One-time setup (already done 2026-04-23)

```bash
git remote add blaine https://github.com/ouday-obeid/loop-cs-agents.git
```

## Weekly cadence (or whenever you want to pull her work)

### Step 1 — Run the verify + cherry-pick script

```bash
bash scripts/backport_from_blaine.sh
```

What it does:
1. Fetches `blaine/main`
2. Classifies each new commit by path-safety (SAFE / MIXED / MANUAL / SKIP)
3. Creates a `from-blaine/<date>-<time>` branch off `main`
4. Cherry-picks every SAFE + MIXED commit onto that branch
5. Runs the verification gate on the review branch:
   - `pytest agents/onboarding/tests/ agents/cs/tests/ tests/` (full coverage: ~400+ tests)
   - `pylint --load-plugins shared.lint.import_rules --disable=all --enable=E9001 agents/onboarding/ agents/cs/` (cross-agent import check)
6. Reports **GREEN** (safe to merge) or **NOT GREEN** (needs human review)

Path classification rules:
| Class | Paths | Action |
|-------|-------|--------|
| SAFE | `agents/onboarding/**`, `agents/cs/**` | Auto cherry-pick |
| MIXED | SAFE + SKIP mixed in one commit | Auto cherry-pick; skip-path noise gets applied but is ignored on upstream |
| MANUAL | `shared/**`, `tests/test_*.py` | Flagged, NOT cherry-picked. You review each. |
| SKIP | `agents/dispatcher/**`, `infra/**`, `pyproject.toml`, `.env.example`, `.claude/**`, `shared/runtime/schedule.py`, `shared/slack_dispatcher.py`, `shared/lint/import_rules.py`, her docs | Never backported. These are intentionally divergent between the two repos. |

### Step 2 — Semantic review (the part the script can't do)

If the script reports GREEN, paste this into a fresh Claude Code session rooted at `~/loop-revops-agents/`:

```
I just ran scripts/backport_from_blaine.sh and it created review branch
from-blaine/<DATE>. Do the following:

1. git log --oneline main..from-blaine/<DATE> — list each cherry-picked commit.

2. For each commit, show me:
   - The diff
   - What behavior changed (not what code changed — what DOES the system do
     differently)
   - Whether the change has test coverage proving the new behavior works
   - Whether it could regress anything else in onboarding/ or cs/
   - Confidence level: high / medium / low

3. Flag any commits where:
   - A public function signature changed (callers may break)
   - A scheduled-job callable path was renamed (launchd plists reference these)
   - A SF field or SObject was added/renamed (needs sandbox-first validation)
   - A governance gate definition changed (approval flow impact)

4. Final verdict per commit: safe-to-merge / needs-changes / reject.

5. Recommended merge order if multiple commits are safe.

Do NOT merge anything. I'll merge manually once I've reviewed your report.
```

### Step 3 — Merge (manual, only after the script is GREEN and Claude's semantic review comes back clean)

```bash
git checkout main
git merge --no-ff from-blaine/<DATE>-<TIME>
git push origin main
git push mini main
```

Then on the Mini:
```bash
ssh mini
cd /Users/jarvis/loop-revops-agents
git pull origin main
# If onboarding or cs callable paths changed, regenerate plists:
bash infra/install_launchd.sh
```

### Step 4 — Tell Blaine

Close the loop in Slack: "Merged <n> commits to upstream, running live on the Mini as of <timestamp>. See `git log` on my side." This lets her know her work is in production.

## What about MANUAL commits?

These touch `shared/**` or shared test files. They're NOT auto-cherry-picked because the shared layer between the two repos has started (or will start) to diverge. Every MANUAL commit needs:

1. Check if our `shared/**` has the same file she modified
2. If identical: `git cherry-pick <sha>` by hand — should apply cleanly
3. If divergent: read both versions, decide whether to take her change, adapt it, or reject
4. If you adapt: cherry-pick to a throwaway branch, edit the file, amend the commit

Never fast-path a MANUAL commit without reading both trees.

## What about SKIP commits?

Never backported. Her repo has intentionally different:
- `agents/dispatcher/main.py` (2-agent; ours registers 7)
- `shared/runtime/schedule.py` (11 jobs; ours has ~31)
- `shared/slack_dispatcher.py` PERSONA_ALIASES map (2 entries; ours has 6)
- `shared/lint/import_rules.py` ORCHESTRATOR_AGENTS (`{"dispatcher", "oo"}` vs our `{"oo"}`)
- `infra/install_launchd.sh` (`com.loop-cs.*` labels vs our `com.loop-revops.*`)
- `pyproject.toml` name, test paths
- `.env.example` (sandbox-only; ours has prod aliases)
- Her Blaine-facing docs (WELCOME, BLAINE_QUICKSTART, HANDOFF, her CLAUDE.md)

If you want to port a philosophy/idea from one of these into upstream, do it as a hand-authored commit here, not a cherry-pick.

## When Blaine wants to pull YOUR changes

If we make improvements upstream (even during the A2–A7 freeze a hotfix might land), Blaine pulls them into her repo the same way:

```bash
cd ~/loop-cs-agents
git remote add upstream https://github.com/ouday-obeid/loop-revops-agents.git
git fetch upstream
# cherry-pick specific commits she wants (never merge upstream/main wholesale —
# her schedule.py/dispatcher/aliases/env would get clobbered)
git cherry-pick <sha>
```

She can mirror this script (`scripts/backport_from_upstream.sh`) in her repo if it becomes a frequent flow. For now it's manual on her side.
