#!/usr/bin/env bash
# Ingest /Users/ottimate/sf-admin/knowledge/*.md into sf_admin corpus.
set -euo pipefail
REPO_ROOT="${REVOPS_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-/Users/ottimate/sf-admin/knowledge}"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate

python <<PY
from pathlib import Path
from shared.mcp import knowledge_mcp
kd = Path("$KNOWLEDGE_DIR")
if not kd.exists():
    print(f"skip: {kd} not found")
    raise SystemExit(0)
count = 0
for p in sorted(kd.glob("*.md")):
    content = p.read_text()
    knowledge_mcp.ingest_document(content, {"id": p.stem, "path": str(p), "source": "sf_admin"}, corpus="sf_admin")
    count += 1
    print(f"  ingested {p.name}")
print(f"seeded {count} docs into sf_admin corpus")
PY
