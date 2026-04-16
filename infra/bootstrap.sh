#!/usr/bin/env bash
# Provision a host for loop-revops-agents. Idempotent.
# Detects MBP (user: ottimate) vs Mac Mini (user: jarvis) and sets REVOPS_REPO_ROOT.

set -euo pipefail

case "${USER:-}" in
  ottimate) DEFAULT_ROOT="/Users/ottimate/loop-revops-agents" ;;
  jarvis)   DEFAULT_ROOT="/Users/jarvis/loop-revops-agents" ;;
  *)        DEFAULT_ROOT="$(cd "$(dirname "$0")/.." && pwd)" ;;
esac

export REVOPS_REPO_ROOT="${REVOPS_REPO_ROOT:-$DEFAULT_ROOT}"
cd "$REVOPS_REPO_ROOT"

echo "[bootstrap] host=$USER repo=$REVOPS_REPO_ROOT"

PY="${PYTHON_BIN:-python3}"
$PY -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"

if [ ! -d .venv ]; then
  echo "[bootstrap] creating venv"
  $PY -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"

if [ ! -f .env ]; then
  echo "[bootstrap] creating .env from template"
  cp .env.example .env
  sed -i '' -e "s|REVOPS_REPO_ROOT=.*|REVOPS_REPO_ROOT=$REVOPS_REPO_ROOT|" .env || true
fi

mkdir -p "$REVOPS_REPO_ROOT/var/log" "$REVOPS_REPO_ROOT/var/chroma" "$REVOPS_REPO_ROOT/var/launchd"

echo "[bootstrap] verifying SF CLI auth (read-only)"
if command -v sf >/dev/null 2>&1; then
  if sf org list --json >/dev/null 2>&1; then
    echo "[bootstrap]   OK: sf org list returned authenticated orgs"
  else
    echo "[bootstrap]   WARN: sf org list failed — run 'sf org login web' before first use" >&2
  fi
else
  echo "[bootstrap]   WARN: sf CLI not on PATH — install via 'brew install sf-cli'" >&2
fi

echo "[bootstrap] verifying Slack bot token (ping O DM)"
if python -c "from shared.slack_dispatcher import SlackSender; SlackSender().ping_o_dm()" 2>/dev/null; then
  echo "[bootstrap]   OK: Slack bot token reached O's DM"
else
  echo "[bootstrap]   WARN: Slack ping failed — check SLACK_BOT_TOKEN in .env" >&2
fi

echo "[bootstrap] done. Activate with: source $REVOPS_REPO_ROOT/.venv/bin/activate"
