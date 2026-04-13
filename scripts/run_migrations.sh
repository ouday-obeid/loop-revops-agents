#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="${REVOPS_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
python -c "from shared.db.connection import init_schema; init_schema(); print('schema ok')"
