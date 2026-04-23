#!/usr/bin/env bash
# backport_from_blaine.sh — pull loop-cs-agents commits into a review branch.
#
# Mental model: Blaine owns `loop-cs-agents` (Onboarder + Supporter). This
# script fetches her main, classifies each new commit by path-safety, and
# lands the safe ones on a review branch in this repo so O can eyeball +
# test them before merging to upstream main.
#
# Path classification:
#   SAFE   — agents/onboarding/**, agents/cs/**        → auto cherry-pick
#   MANUAL — shared/**, tests/test_*.py (excl. dispatcher-specific)
#            → flagged, NOT cherry-picked; O decides commit-by-commit
#   SKIP   — agents/dispatcher/**, infra/**, pyproject.toml, .env.example,
#            .claude/**, docs Blaine authored (WELCOME/BLAINE_*/HANDOFF),
#            shared/runtime/schedule.py, shared/slack_dispatcher.py
#            (intentionally divergent — never backport)
#
# Usage:
#   bash scripts/backport_from_blaine.sh                # review mode (default)
#   bash scripts/backport_from_blaine.sh --dry-run      # classify only, no branch
#
# After the review branch exists, you merge it manually:
#   git checkout main
#   git merge --no-ff from-blaine/<date>
#   git push origin main && git push mini main
#
# The script NEVER touches `main` directly — upstream is authoritative and O
# decides what merges in.
set -euo pipefail

MODE="${1:-review}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if ! git remote | grep -q '^blaine$'; then
  echo "[backport] ERROR: no 'blaine' remote configured." >&2
  echo "  Fix: git remote add blaine https://github.com/ouday-obeid/loop-cs-agents.git" >&2
  exit 1
fi

echo "[backport] fetching blaine/main"
git fetch blaine --quiet

# Find commits on blaine/main that aren't in our main.
NEW_COMMITS=$(git log --reverse --format='%H' main..blaine/main || true)
if [ -z "$NEW_COMMITS" ]; then
  echo "[backport] blaine/main has no new commits vs main. Nothing to do."
  exit 0
fi

COUNT=$(echo "$NEW_COMMITS" | wc -l | tr -d ' ')
echo "[backport] $COUNT new commit(s) on blaine/main:"
echo ""
git log --format='  %h  %s  (%an, %ar)' main..blaine/main
echo ""

# Classification regexes. Arrays would be nicer but this is portable bash.
is_safe() {
  case "$1" in
    agents/onboarding/*|agents/cs/*) return 0 ;;
    *) return 1 ;;
  esac
}
is_skip() {
  case "$1" in
    agents/dispatcher/*) return 0 ;;
    agents/oo/*) return 0 ;;
    infra/*) return 0 ;;
    .claude/*) return 0 ;;
    .gitignore|.env.example) return 0 ;;
    pyproject.toml) return 0 ;;
    README.md|CLAUDE.md|HANDOFF.md|BLAINE_QUICKSTART.md|WELCOME.md) return 0 ;;
    docs/blaine_intake_*.md) return 0 ;;
    shared/runtime/schedule.py) return 0 ;;
    shared/runtime/launchd/generate.py) return 0 ;;
    shared/slack_dispatcher.py) return 0 ;;
    shared/lint/import_rules.py) return 0 ;;  # ORCHESTRATOR_AGENTS divergence
    *) return 1 ;;
  esac
}

# Classify each commit. A commit is:
#   SAFE   — every touched path is_safe
#   SKIP   — every touched path is_skip
#   MIXED  — some SAFE + some SKIP (can cherry-pick, subject has skip noise)
#   MANUAL — any path matches neither (usually shared/**)
classify_commit() {
  local sha="$1"
  local files
  files=$(git show --name-only --format='' "$sha" | sed '/^$/d')
  local saw_safe=0 saw_skip=0 saw_manual=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if is_safe "$f"; then saw_safe=1
    elif is_skip "$f"; then saw_skip=1
    else saw_manual=1
    fi
  done <<< "$files"
  if [ $saw_manual -eq 1 ]; then echo MANUAL
  elif [ $saw_safe -eq 1 ] && [ $saw_skip -eq 1 ]; then echo MIXED
  elif [ $saw_safe -eq 1 ]; then echo SAFE
  else echo SKIP
  fi
}

SAFE_SHAS=()
MIXED_SHAS=()
MANUAL_SHAS=()
SKIP_SHAS=()

echo "[backport] classifying:"
while IFS= read -r sha; do
  [ -z "$sha" ] && continue
  cls=$(classify_commit "$sha")
  short=$(git log -1 --format='%h %s' "$sha")
  printf "  %-7s %s\n" "[$cls]" "$short"
  case "$cls" in
    SAFE)   SAFE_SHAS+=("$sha") ;;
    MIXED)  MIXED_SHAS+=("$sha") ;;
    MANUAL) MANUAL_SHAS+=("$sha") ;;
    SKIP)   SKIP_SHAS+=("$sha") ;;
  esac
done <<< "$NEW_COMMITS"
echo ""

echo "[backport] summary:"
echo "  SAFE   (auto cherry-pick):          ${#SAFE_SHAS[@]}"
echo "  MIXED  (cherry-pick w/ skip-paths): ${#MIXED_SHAS[@]}"
echo "  MANUAL (shared/** — review first):  ${#MANUAL_SHAS[@]}"
echo "  SKIP   (never backport):            ${#SKIP_SHAS[@]}"
echo ""

if [ ${#MANUAL_SHAS[@]} -gt 0 ]; then
  echo "[backport] MANUAL commits (NOT cherry-picked — review each, cherry-pick by hand if wanted):"
  for sha in "${MANUAL_SHAS[@]}"; do
    git log -1 --format='  %h %s' "$sha"
    git show --name-only --format='' "$sha" | sed 's/^/    /'
  done
  echo ""
fi

if [ "$MODE" = "--dry-run" ]; then
  echo "[backport] dry-run — exiting without creating review branch."
  exit 0
fi

if [ ${#SAFE_SHAS[@]} -eq 0 ] && [ ${#MIXED_SHAS[@]} -eq 0 ]; then
  echo "[backport] no SAFE or MIXED commits to cherry-pick. Exiting."
  exit 0
fi

BRANCH="from-blaine/$(date +%Y-%m-%d-%H%M)"
echo "[backport] creating review branch: $BRANCH"
git checkout -b "$BRANCH" main

CHERRY_OK=()
CHERRY_FAIL=()
for sha in "${SAFE_SHAS[@]}" "${MIXED_SHAS[@]}"; do
  short=$(git log -1 --format='%h' "$sha")
  echo "[backport] cherry-pick $short"
  if git cherry-pick -x "$sha" --strategy-option=theirs >/dev/null 2>&1; then
    CHERRY_OK+=("$sha")
  else
    echo "[backport]   conflict — aborting cherry-pick"
    git cherry-pick --abort 2>/dev/null || true
    CHERRY_FAIL+=("$sha")
  fi
done

echo ""
echo "[backport] cherry-pick results:"
echo "  OK:   ${#CHERRY_OK[@]}"
echo "  FAIL: ${#CHERRY_FAIL[@]}"
if [ ${#CHERRY_FAIL[@]} -gt 0 ]; then
  echo "  Failed SHAs (apply manually):"
  for sha in "${CHERRY_FAIL[@]}"; do
    git log -1 --format='    %h %s' "$sha"
  done
fi
echo ""

if [ ${#CHERRY_OK[@]} -eq 0 ]; then
  echo "[backport] nothing applied — leaving branch empty for manual work."
  exit 0
fi

# Run the verification gate.
echo "[backport] running verification gate (tests + lint)"
if [ ! -d .venv ]; then
  echo "[backport]   no .venv — skipping tests. Run: bash infra/bootstrap.sh"
  exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate

TEST_RESULT=0
LINT_RESULT=0

echo ""
echo "[backport]   pytest (agents/onboarding + agents/cs + tests/)"
if pytest -q \
     agents/onboarding/tests/ \
     agents/cs/tests/ \
     tests/ \
     2>&1 | tee /tmp/backport_pytest.log | tail -5; then
  echo "[backport]   tests PASSED"
else
  TEST_RESULT=1
  echo "[backport]   tests FAILED — see /tmp/backport_pytest.log"
fi

echo ""
echo "[backport]   pylint cross-agent check"
if pylint --load-plugins shared.lint.import_rules --disable=all --enable=E9001 \
     agents/onboarding/ agents/cs/ 2>&1 | tee /tmp/backport_pylint.log | tail -5; then
  echo "[backport]   lint PASSED"
else
  LINT_RESULT=1
  echo "[backport]   lint FAILED — see /tmp/backport_pylint.log"
fi

echo ""
echo "=========================================================================="
echo "[backport] review branch:   $BRANCH"
echo "[backport] commits applied: ${#CHERRY_OK[@]}"
echo "[backport] tests:           $([ $TEST_RESULT -eq 0 ] && echo PASS || echo FAIL)"
echo "[backport] lint:            $([ $LINT_RESULT -eq 0 ] && echo PASS || echo FAIL)"
echo ""
if [ $TEST_RESULT -eq 0 ] && [ $LINT_RESULT -eq 0 ] && [ ${#CHERRY_FAIL[@]} -eq 0 ] && [ ${#MANUAL_SHAS[@]} -eq 0 ]; then
  echo "[backport] GREEN — safe to merge:"
  echo "  git checkout main"
  echo "  git merge --no-ff $BRANCH"
  echo "  git push origin main && git push mini main"
else
  echo "[backport] NOT GREEN — do semantic review before merging."
  echo "  Review branch with:"
  echo "    git log --oneline main..$BRANCH"
  echo "    git diff main..$BRANCH -- agents/onboarding agents/cs"
fi
echo "=========================================================================="
