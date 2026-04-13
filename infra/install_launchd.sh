#!/usr/bin/env bash
# Install launchd plists for all jobs in shared/runtime/schedule.py.
# Idempotent: unloads existing labels before loading fresh.

set -euo pipefail

REPO_ROOT="${REVOPS_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

python -m shared.runtime.launchd.generate --out "$REPO_ROOT/var/launchd" --repo-root "$REPO_ROOT"

TARGET_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$TARGET_DIR"

for plist in "$REPO_ROOT"/var/launchd/com.loop-revops.*.plist; do
  name=$(basename "$plist")
  label="${name%.plist}"
  dest="$TARGET_DIR/$name"
  cp "$plist" "$dest"
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID" "$dest"
  echo "[launchd] loaded $label"
done

echo "[launchd] active loop-revops agents:"
launchctl list | grep loop-revops || echo "  (none yet — will appear after next scheduled trigger)"
