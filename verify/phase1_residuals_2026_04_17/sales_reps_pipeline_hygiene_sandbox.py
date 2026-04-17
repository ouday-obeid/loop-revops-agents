"""Sales Reps pipeline_hygiene sandbox run — Phase 1 residual.

Runs the daily hygiene sweep against the RevAgents sandbox (not prod) to prove
the module executes end-to-end with the FIX from v0.4/v0.9.1. Captures the full
Slack payload + findings breakdown as an artifact.

Run:
    cd ~/loop-revops-agents
    python3 -m verify.phase1_residuals_2026_04_17.sales_reps_pipeline_hygiene_sandbox
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Force sandbox alias for this run; keep dev guard on so no Slack posts escape.
os.environ["SF_ORG_ALIAS"] = "revagents"
os.environ.setdefault("SLACK_DEV_GUARD", "1")
os.environ.setdefault("SLACK_TEST_CHANNEL", "U08K2UTG3G8")


async def main() -> int:
    from agents.sales_reps import pipeline_hygiene

    result = await pipeline_hygiene.run(ae_filter=None, stale_days=14, preview_per_ae=5)

    artifact = Path(__file__).parent / f"sales_reps_pipeline_hygiene_sandbox_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    artifact.write_text(json.dumps(result, indent=2, default=str))
    print(f"artifact: {artifact}")
    print(f"total_findings: {result.get('total_findings', 'n/a')}")
    print(f"totals_by_issue: {result.get('totals_by_issue')}")
    if "error" in result:
        print(f"ERROR: {result['error']}")
        return 1
    # Short Slack-payload preview
    text = result.get("text", "")
    for line in text.split("\n")[:8]:
        print(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
