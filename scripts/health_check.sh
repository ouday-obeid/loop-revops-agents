#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="${REVOPS_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

python -c "
import asyncio
from agents.oo.integration_health import poll
import json
print(json.dumps(asyncio.run(poll()), indent=2, default=str))
"
