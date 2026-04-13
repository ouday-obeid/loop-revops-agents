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

echo "[bootstrap] done. Activate with: source $REVOPS_REPO_ROOT/.venv/bin/activate"
